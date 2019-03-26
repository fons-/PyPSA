#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Feb 21 12:14:49 2018

@author: fabian
"""

# This side-package is created for use as flow and cost allocation.

from .pf import calculate_PTDF, find_cycles
from pandas import IndexSlice as idx
import pandas as pd
import numpy as np
import scipy as sp
from collections import Iterable
import logging
import os
from numpy import sign

logger = logging.getLogger(__name__)


# %% linalg

def pinv(df):
    return pd.DataFrame(np.linalg.pinv(df), df.columns, df.index)

def inv(df):
    return pd.DataFrame(np.linalg.inv(df), df.columns, df.index)


def null(df):
    if df.empty:
        return df
    return pd.DataFrame(sp.linalg.null_space(df), index=df.columns)


def diag(df):
    """
    Convenience function to select diagonal from a square matrix, or to build
    a diagonal matrix from a series.

    Parameters
    ----------
    df : pandas.DataFrame or pandas.Series
    """
    if isinstance(df, pd.DataFrame):
        if len(df.columns) == len(df.index) > 1:
            return pd.DataFrame(np.diagflat(np.diag(df)), df.index, df.columns)
    return pd.DataFrame(np.diagflat(df.values),
                        index=df.index, columns=df.index)



def eig(M):
    val, vec = np.linalg.eig(M)
    val = pd.Series(val).sort_values(ascending=False)
    vec = pd.DataFrame(vec, index=M.index).reindex(columns=val.index)
    return val, vec

#%% graph and power flow

def incidence_matrix(n, branch_components=['Link', 'Line']):
    buses = n.buses.index
    return pd.concat([(n.df(c).assign(K=1).set_index('bus0', append=True)['K']
                     .unstack().reindex(columns=buses).fillna(0).T)
                     - (n.df(c).assign(K=1).set_index('bus1', append=True)['K']
                     .unstack().reindex(columns=buses).fillna(0).T)
                     for c in branch_components],
                     keys=branch_components, axis=1, sort=False)\
            .reindex(columns=n.branches().loc[branch_components].index)\
            .rename_axis(columns=['component', 'branch_i'])

def cycles(n, dense=True, update=True):
    if (not 'C' in n.__dir__()) | update:
        find_cycles(n, dense=dense)
        return n.C.T
    else:
        return n.C.T

def active_cycles(n, snapshot):
#    f = pd.concat([n.lines_t.p0.loc[snapshot], n.links_t.p0.loc[snapshot]],
#              keys=['Line', 'Link'])

    # copy original links
    orig_links = n.links.copy()
    # modify current links
    n.links = n.links[n.links_t.p0.loc[snapshot].abs() >= 1e-8]
    C = cycles(n, update=True)
    # reassign original links
    n.links = orig_links
    return C.reindex(columns=n.branches().index, fill_value=0)


def impedance(n, branch_components=['Line', 'Link'], snapshot=None):
    n.lines = n.lines.assign(carrier=n.lines.bus0.map(n.buses.carrier))

    #impedance
    _z = [n.lines.x_pu_eff.where(n.lines.carrier == 'AC', n.lines.r_pu_eff)]
    z = pd.concat(_z, keys=["Line"])

    if (branch_components == ['Line']) | n.links.empty :
        return z
    if snapshot is None:
        logger.warn('Link in argument "branch_components", but no '
                        'snapshot given. Falling back to first snapshot')
        snapshot = n.snapshots[0]
    elif isinstance(snapshot, pd.DatetimeIndex):
        snapshot = snapshot[0]

    f = pd.concat([n.lines_t.p0.loc[snapshot], n.links_t.p0.loc[snapshot]],
                  keys=['Line', 'Link'])
    n.lines = n.lines.assign(carrier=n.lines.bus0.map(n.buses.carrier))

    C = active_cycles(n, snapshot)

    C_mix = C[((( C != 0) & (f != 0)).groupby(level=0, axis=1).any()).Link]

    if C_mix.empty:
        omega = f[['Link']]
    elif z.empty:
        omega = null(C_mix[['Link']] @ diag(f[['Link']]))[0]
    else:
        omega = - pinv(C_mix[['Link']] @ diag(f[['Link']])) \
                @ C_mix[['Line']] @ diag(z) @ f[['Line']]

    omega = omega.round(10) #numerical issues either
    omega[(omega == 0) & (f[['Link']] != 0)] = (1/f).fillna(0)
    return pd.concat([z, omega]).loc[branch_components]


def admittance(n, branch_components=['Line', 'Link'], snapshot=None):
    return (1/impedance(n, branch_components, snapshot))\
            .replace([np.inf, -np.inf], 0)


def PTDF(n, branch_components=['Line'], snapshot=None):
    n.calculate_dependent_values()
    K = incidence_matrix(n, branch_components)
    y = admittance(n, branch_components, snapshot)
    return diag(y) @ K.T @ pinv(K @ diag(y) @ K.T)


def Ybus(n, branch_components=['Line', 'Link'], snapshot=None):
    K = incidence_matrix(n, branch_components)
    y = admittance(n, branch_components, snapshot)
    return K @ diag(y) @ K.T


def Zbus(n, branch_components=['Line', 'Link'], snapshot=None):
    return pinv(Ybus(n, branch_components, snapshot))


# %% power system

def network_injection(n, snapshots=None, branch_components=['Link', 'Line']):
    """
    Function to determine the total network injection including passive and
    active branches.
    """
    if snapshots is None:
        snapshots = n.snapshots
    if isinstance(snapshots, pd.Timestamp):
        snapshots = [snapshots]
    if branch_components == ['Line']:
        return n.buses_t.p.loc[snapshots]
    elif sorted(branch_components) == ['Line', 'Link']:
        if 'p_n' not in n.buses_t.keys():
            n.buses_t['p_n'] = (sum(n.pnl(l)['p{}'.format(i)]
                                .groupby(n.df(l)['bus{}'.format(i)], axis=1)
                                .sum()
                                .reindex(columns=n.buses.index, fill_value=0)
                                for l in ['Link', 'Line'] for i in [0, 1])
                                .round(10))
        return n.buses_t['p_n'].loc[snapshots].rename_axis('snapshot')
    elif branch_components == ['Link']:
        if 'p_link' not in n.buses_t.keys():
            n.buses_t['p_link'] = (sum(n.pnl(l)['p{}'.format(i)]
                                   .groupby(n.df(l)['bus{}'.format(i)], axis=1)
                                   .sum()
                                   .reindex(columns=n.buses.index,
                                            fill_value=0)
                                   for l in ['Link'] for i in [0, 1])
                                   .round(10))
        return n.buses_t['p_link'].loc[snapshots].rename_axis('snapshot')


def is_balanced(n, tol=1e-9):
    """
    Helper function to double check whether network flow is balanced
    """
    K = incidence_matrix(n)
    F = pd.concat([n.lines_t.p0, n.links_t.p0], axis=1,
                  keys=['Line', 'Link']).T
    return (K.dot(F)).sum(0).max() < tol


def power_production(n, snapshots=None,
                     components=['Generator', 'StorageUnit'],
                     per_carrier=False, update=False):
    if snapshots is None:
        snapshots = n.snapshots.rename('snapshot')
    if 'p_plus' not in n.buses_t or update:
        n.buses_t.p_plus = (sum(n.pnl(c).p
                            .mul(n.df(c).sign).T
                            .clip(lower=0)
                            .assign(bus=n.df(c).bus)
                            .groupby('bus').sum()
                            .reindex(index=n.buses.index, fill_value=0).T
                            for c in components)
                            .rename_axis('source', axis=1))
    if 'p_plus_per_carrier' not in n.buses_t or update:
        n.buses_t.p_plus_per_carrier = (
                pd.concat([(n.pnl(c).p.T
                            .assign(carrier=n.df(c).carrier, bus=n.df(c).bus)
                            .groupby(['bus', 'carrier']).sum().T
                            .where(lambda x: x > 0))
                          for c in components], axis=1)
                .rename_axis(['source', 'sourcetype'], axis=1))

    if per_carrier:
        return n.buses_t.p_plus_per_carrier.reindex(snapshots)
    return n.buses_t.p_plus.reindex(snapshots)


def power_demand(n, snapshots=None,
                 components=['Load', 'StorageUnit'],
                 per_carrier=False, update=False):
    if snapshots is None:
        snapshots = n.snapshots.rename('snapshot')
    if 'p_minus' not in n.buses_t or update:
        n.buses_t.p_minus = (sum(n.pnl(c).p.T
                             .mul(n.df(c).sign, axis=0)
                             .clip(upper=0)
                             .assign(bus=n.df(c).bus)
                             .groupby('bus').sum()
                             .reindex(index=n.buses.index, fill_value=0).T
                             for c in components).abs()
                             .rename_axis('sink', axis=1))

    if 'p_minus_per_carrier' not in n.buses_t or update:
        if components == ['Generator', 'StorageUnit']:
            intersc = (pd.Index(n.storage_units.carrier.unique())
                       .intersection(pd.Index(n.generators.carrier.unique())))
            assert (intersc.empty), (
                    'Carrier names {} of compoents are not unique'
                    .format(intersc))
        n.loads = n.loads.assign(carrier='load')
        n.buses_t.p_minus_per_carrier = -(
                pd.concat([(n.pnl(c).p.T
                .mul(n.df(c).sign, axis=0)
                .assign(carrier=n.df(c).carrier, bus=n.df(c).bus)
                .groupby(['bus', 'carrier']).sum().T
                .where(lambda x: x < 0)) for c in components], axis=1)
                .rename_axis(['sink', 'sinktype'], axis=1))
        n.loads = n.loads.drop(columns='carrier')

    if per_carrier:
        return n.buses_t.p_minus_per_carrier.reindex(snapshots)
    return n.buses_t.p_minus.reindex(snapshots)


def self_consumption(n, snapshots=None, override=False):
    """
    Inspection for self consumed power, i.e. power that is not injected in the
    network and consumed by the bus itself
    """
    if snapshots is None:
        snapshots = n.snapshots.rename('snapshot')
    if 'p_self' not in n.buses_t or override:
        n.buses_t.p_self = (pd.concat([power_production(n, n.snapshots),
                                       power_demand(n, n.snapshots)], axis=1)
                            .groupby(level=0, axis=1).min())
    return n.buses_t.p_self.loc[snapshots]


def expand_by_source_type(ds, n, components=['Generator', 'StorageUnit'],
                          as_categoricals=True, use_dask=False,
                          cut_lower_share=1e-5):
    """
    Breakdown allocation into generation carrier type. These include carriers
    of all components specified by 'components'. Note that carrier names of all
    components have to be unique.

    Pararmeter
    ----------

    ds : pd.Series
        Allocation Series with at least index level 'source'
    n : pypsa.Network()
        Network which the allocation was derived from
    components : list, default ['Generator', 'StorageUnit']
        List of considered components. Carrier types of these components are
        taken for breakdown.


    Example
    -------

    ap = flow_allocation(n, n.snapshots, per_bus=True)
    ap_carrier = pypsa.allocation.expand_by_carrier(ap, n)

    """
    sns = ds.index.unique('snapshot')
    share_per_bus_carrier = power_production(n, sns, per_carrier=True) \
                              .div(power_production(n, sns), level='source').T \
                              [lambda x: x>cut_lower_share] \
                              .stack() \
                              .reorder_levels(['snapshot', 'source',
                                               'sourcetype'])
    return (share_per_bus_carrier * ds).dropna().rename('allocation')


def expand_by_sink_type(ds, n, components=['Load', 'StorageUnit'],
                        as_categoricals=True, use_dask=False,
                        cut_lower_share=1e-5):
    """
    Breakdown allocation into demand types, e.g. Storage carriers and Load.
    These include carriers of all components specified by 'components'. Note
    that carrier names of all components have to be unique.

    Pararmeter
    ----------

    ds : pd.Series
        Allocation Series with at least index level 'sink'
    n : pypsa.Network()
        Network which the allocation was derived from
    components : list, default ['Load', 'StorageUnit']
        List of considered components. Carrier types of these components are
        taken for breakdown.


    Example
    -------

    ap = flow_allocation(n, n.snapshots, per_bus=True)
    ap_carrier = pypsa.allocation.expand_by_carrier(ap, n)

    """
    sns = ds.index.unique('snapshot')
    share_per_bus_carrier = power_demand(n, sns, per_carrier=True) \
                             .div(power_demand(n, sns), level='sink').T \
                             [lambda x: x>cut_lower_share] \
                             .stack() \
                             .reorder_levels(['snapshot', 'sink', 'sinktype'])
    return (share_per_bus_carrier * ds).dropna().rename('allocation')

# %% Helper functions, not the right place in this module, but okay

compute_if_dask = lambda df, b: df.compute() if b else df

def to_dask(df, use_dask=False):
    if use_dask:
        import dask.dataframe as dd
        if df.index.names[0] == 'snapshot':
            return dd.from_pandas(df, npartitions=1).repartition(freq='1m')
        else:
            npartitions = 1+df.memory_usage(deep=True).sum() // 100e6
            return dd.from_pandas(df, npartitions=npartitions)
    else:
        return df


def _to_categorical_index(df, axis=0):

    def to_cat_if_obj(i):
        return i.astype('category') if i.is_object() else i

    if df.axes[axis].nlevels > 1:
        return df.set_axis(
                df.axes[axis]
                    .set_levels([to_cat_if_obj(i) for i
                                 in df.axes[axis].levels]),
                inplace=False, axis=axis)
    else:
        if df.axes[axis].is_object():
            return df.set_axis(to_cat_if_obj(df.axes[axis]),
                inplace=False, axis=axis)


def _sync_categrorical_axis(df1, df2, axis=0):
    overlap_levels = [n for n in df1.axes[axis].names
                      if n in df2.axes[axis].names and
                      (df1.axes[axis].unique(n).is_categorical() &
                       df2.axes[axis].unique(n).is_categorical())]
    union = [df1.axes[axis].unique(n).union(df2.axes[axis].unique(n))
             .categories for n in overlap_levels]
    for level, cats in zip(overlap_levels, union):
        df1 = df1.pipe(_set_categories_for_level, level, cats, axis=axis)
        df2 = df2.pipe(_set_categories_for_level, level, cats, axis=axis)


def _set_categories_for_level(df, level, categories, axis=0):
    level = [level] if isinstance(level, str) else level
    return df.set_axis(
            df.axes[axis].set_levels([i.set_categories(categories)
            if i.name in level else i for i in df.axes[axis].levels]),
        inplace=False, axis=axis)


def set_cats(df, n=None, axis=0):
    """
    Helper function for converting index of allocation series to categoricals.
    If a network is passed the categories will be aligned to the components
    of the network.
    """
    if n is None:
        return df.pipe(_to_categorical_index, axis=axis)
    buses = n.buses.index
    branch_i = n.branches().index.levels[1]
    bus_lv_names = ['sink', 'source', 'bus0', 'bus1', 'in', 'out']
    return df.pipe(_to_categorical_index, axis=axis)\
             .pipe(_set_categories_for_level, bus_lv_names, buses, axis=axis)\
             .pipe(_set_categories_for_level, ['branch_i'],
                   branch_i, axis=axis)

def droplevel(df, levels, axis=0):
    ax = df.axes[axis]
    for level in levels:
        ax = ax.droplevel(level)
    return df.set_axis(ax, axis=axis, inplace=False)


def parmap(f, arg_list, nprocs=None, **kwargs):
    import multiprocessing

    def fun(f, q_in, q_out):
        while True:
            i, x = q_in.get()
            if i is None:
                break
            q_out.put((i, f(x)))

    if nprocs is None:
        nprocs = multiprocessing.cpu_count()
    logger.info('Run process with {} parallel threads.'.format(nprocs))
    q_in = multiprocessing.Queue(1)
    q_out = multiprocessing.Queue()

    proc = [multiprocessing.Process(target=fun, args=(f, q_in, q_out))
            for _ in range(nprocs)]
    for p in proc:
        p.daemon = True
        p.start()

    sent = [q_in.put((i, x)) for i, x in enumerate(arg_list)]
    [q_in.put((None, None)) for _ in range(nprocs)]
    res = [q_out.get() for _ in range(len(sent))]
    [p.join() for p in proc]
    return [x for i, x in sorted(res)]


# %% allocation methods


def average_participation(n, snapshot, per_bus=False, normalized=False,
                          downstream=True, branch_components=['Line', 'Link'],
                          aggregated=True):
    """
    Allocate the network flow in according to the method 'Average
    participation' or 'Flow tracing' firstly presented in [1,2].
    The algorithm itself is derived from [3]. The general idea is to
    follow active power flow from source to sink (or sink to source)
    using the principle of proportional sharing and calculate the
    partial flows on each line, or to each bus where the power goes
    to (or comes from).

    This method provdes two general options:
        Downstream:
            The flow of each nodal power injection is traced through
            the network and decomposed the to set of lines/buses
            on which is flows on/to.
        Upstream:
            The flow of each nodal power demand is traced
            (in reverse direction) through the network and decomposed
            to the set of lines/buses where it comes from.

    [1] J. Bialek, “Tracing the flow of electricity,”
        IEE Proceedings - Generation, Transmission and Distribution,
        vol. 143, no. 4, p. 313, 1996.
    [2] D. Kirschen, R. Allan, G. Strbac, Contributions of individual
        generators to loads and flows, Power Systems, IEEE
        Transactions on 12 (1) (1997) 52–60. doi:10.1109/59.574923.
    [3] J. Hörsch, M. Schäfer, S. Becker, S. Schramm, and M. Greiner,
        “Flow tracing as a tool set for the analysis of networked
        large-scale renewable electricity systems,” International
        Journal of Electrical Power & Energy Systems,
        vol. 96, pp. 390–397, Mar. 2018.



    Parameters
    ----------
    network : pypsa.Network() object with calculated flow data

    snapshot : str
        Specify snapshot which should be investigated. Must be
        in network.snapshots.
    per_bus : Boolean, default True
        Whether to return allocation on buses. Allocate to lines
        if False.
    normalized : Boolean, default False
        Return the share of the source (sink) flow
    downstream : Boolean, default True
        Whether to use downstream or upstream method.

    """
    lower = lambda df: df.clip(upper=0)
    upper = lambda df: df.clip(lower=0)


    f0 = pd.concat([n.pnl(c).p0.loc[snapshot] for c in branch_components],
                  keys=branch_components, sort=True) \
          .rename_axis(['component', 'branch_i'])
    f1 = pd.concat([n.pnl(c).p1.loc[snapshot] for c in branch_components],
                  keys=branch_components, sort=True) \
          .rename_axis(['component', 'branch_i'])

    f_in = f0.where(f0 > 0, - f1)
    f_out = f0.where(f0 < 0,  - f1)


    p = network_injection(n, snapshot, branch_components).T
    if aggregated:
        p_in = p.clip(lower=0)  # nodal inflow
        p_out = - p.clip(upper=0)  # nodal outflow
    else:
        p_in = power_production(n, [snapshot]).loc[snapshot]
        p_out = power_demand(n, [snapshot]).loc[snapshot]

    K = incidence_matrix(n, branch_components)

    K_dir = K @ diag(sign(f_in))

#    Tau = lower(K_loss_dir) * f @ K.T + diag(p_in)

    Q = inv(lower(K_dir) @ diag(f_out) @ K.T + diag(p_in)) @ diag(p_in)
    R = inv(upper(K_dir) @ diag(f_in) @ K.T + diag(p_out)) @ diag(p_out)

    if not normalized and per_bus:
        Q = diag(p_out) @ Q
        R = diag(p_in) @ R
        if aggregated:
            # add self-consumption
            Q += diag(self_consumption(n, snapshot))
            R += diag(self_consumption(n, snapshot))

    q = (Q.rename_axis('in').rename_axis('source', axis=1)
         .replace(0, np.nan)
         .stack().swaplevel(0)#.sort_index()
         .rename('upstream'))#.pipe(set_cats, n))

    r = (R.rename_axis('out').rename_axis('sink', axis=1)
         .replace(0, np.nan)
         .stack()#.sort_index()
         .rename('downstream'))#.pipe(set_cats, n))


    if per_bus:
        T = (pd.concat([q,r], axis=0, keys=['upstream', 'downstream'],
                       names=['method', 'source', 'sink'])
             .rename('allocation'))
        if downstream is not None:
            T = T.downstream if downstream else T.upstream

    else:
        f = f_in if downstream else f_out

        f = (n.branches().loc[branch_components]
               .assign(flow=f)
               .rename_axis(['component', 'branch_i'])
               .set_index(['bus0', 'bus1'], append=True)['flow'])
#               .pipe(set_cats, n)

        # absolute flow with directions
        f_dir = pd.concat(
                [f[f > 0].rename_axis(index={'bus0':'in', 'bus1': 'out'}),
                 f[f < 0].swaplevel()
                         .rename_axis(index={'bus0':'out', 'bus1': 'in'})])


        if normalized:
            f_dir = (f_dir.groupby(level=['component', 'branch_i'])
                     .transform(lambda ds: ds/ds.abs().sum()))

        T = (q * f_dir * r).dropna() \
            .droplevel(['in', 'out'])\
            .reorder_levels(['source', 'sink', 'component', 'branch_i'])\
            .rename('allocation')

    return pd.concat([T], keys=[snapshot], names=['snapshot'])


def marginal_participation(n, snapshot=None, q=0.5, normalized=False,
                           per_bus=False):
    '''
    Allocate line flows according to linear sensitvities of nodal power
    injection given by the changes in the power transfer distribution
    factors (PTDF)[1-3]. As the method is based on the DC-approximation,
    it works on subnetworks only as link flows are not taken into account.
    Note that this method does not exclude counter flows.

    [1] F. J. Rubio-Oderiz, I. J. Perez-Arriaga, Marginal pricing of
        transmission services: a comparative analysis of network cost
        allocation methods, IEEE Transactions on Power Systems 15 (1)
        (2000) 448–454. doi:10.1109/59.852158.
    [2] M. Schäfer, B. Tranberg, S. Hempel, S. Schramm, M. Greiner,
        Decompositions of injection patterns for nodal flow allocation
        in renewable electricity networks, The European Physical
        Journal B 90 (8) (2017) 144.
    [3] T. Brown, “Transmission network loading in Europe with high
        shares of renewables,” IET Renewable Power Generation,
        vol. 9, no. 1, pp. 57–65, Jan. 2015.


    Parameters
    ----------
    network : pypsa.Network() object with calculated flow data

    snapshot : str
        Specify snapshot which should be investigated. Must be
        in network.snapshots.
    q : float, default 0.5
        split between net producers and net consumers.
        If q is zero, only the impact of net load is taken into
        account. If q is one, only net generators are taken
        into account.
    per_bus : Boolean, default True
        Whether to return allocation on buses. Allocate to lines
        if False.
    normalized : Boolean, default False
        Return the share of the source (sink) flow

    '''
    snapshot = n.snapshots[0] if snapshot is None else snapshot
    H = PTDF(n)
    p = n.buses_t.p.loc[snapshot]
    p_plus = p.clip(lower=0)
    p_minus = p.clip(upper=0)
    f = n.lines_t.p0.loc[snapshot]
#   unbalanced flow from positive injection:
    f_plus = H @ p_plus
    f_minus = H @ p_minus
    k_plus = (q * f - f_plus) / p_plus.sum()
    if normalized:
        Q = H.add(k_plus, axis=0).mul(p, axis=1).div(f, axis=0).round(10).T
    else:
        Q = H.add(k_plus, axis=0).mul(p, axis=1).round(10).T
    if per_bus:
        K = incidence_matrix(n, branch_components=['Line'])
        Q = K @ Q.T
        Q = (Q.rename_axis('source').rename_axis('sink', axis=1)
             .stack().round(8)[lambda ds:ds != 0])
    else:
        Q = (Q.rename_axis('bus')
             .rename_axis(['component', 'branch_i'], axis=1)
             .unstack()
             .round(8)[lambda ds:ds != 0]
             .reorder_levels(['bus', 'component', 'branch_i'])
             .sort_index())
    return pd.concat([Q], keys=[snapshot], names=['snapshot'])


def virtual_injection_pattern(n, snapshot=None, normalized=False, per_bus=False,
                              downstream=True):
    """
    Sequentially calculate the load flow induced by individual
    power sources in the network ignoring other sources and scaling
    down sinks. The sum of the resulting flow of those virtual
    injection patters is the total network flow. This method matches
    the 'Marginal participation' method with q = 1.



    Parameters
    ----------
    network : pypsa.Network object with calculated flow data
    snapshot : str
        Specify snapshot which should be investigated. Must be
        in network.snapshots.
    per_bus : Boolean, default True
        Whether to return allocation on buses. Allocate to lines
        if False.
    normalized : Boolean, default False
        Return the share of the source (sink) flow

    """
    snapshot = n.snapshots[0] if snapshot is None else snapshot
    H = PTDF(n)
    p = n.buses_t.p.loc[snapshot]
    p_plus = p.clip(lower=0)
    p_minus = p.clip(upper=0)
    f = n.lines_t.p0.loc[snapshot]
    if downstream:
        indiag = diag(p_plus)
        offdiag = (p_minus.to_frame().dot(p_plus.to_frame().T)
                   .div(p_plus.sum()))
    else:
        indiag = diag(p_minus)
        offdiag = (p_plus.to_frame().dot(p_minus.to_frame().T)
                   .div(p_minus.sum()))
    vip = indiag + offdiag
    if per_bus:
        Q = (vip[indiag.sum() == 0].T
             .rename_axis('sink', axis=int(downstream))
             .rename_axis('source', axis=int(not downstream))
             .stack()[lambda ds:ds != 0]).abs()
#        switch to counter stream by Q.swaplevel(0).sort_index()
    else:
        Q = H.dot(vip).round(10).T
        if normalized:
            # normalized colorvectors
            Q /= f
        Q = (Q.rename_axis('bus') \
              .rename_axis(["component", 'branch_i'], axis=1)
              .unstack().round(8)
              .reorder_levels(['bus', 'component', 'branch_i'])
              .sort_index()
              [lambda ds: ds != 0])
    return pd.concat([Q], keys=[snapshot], names=['snapshot'])


def optimal_flow_shares(n, snapshot, method='min', downstream=True,
                        per_bus=False, **kwargs):
    """



    """
    from scipy.optimize import minimize
    H = PTDF(n)
    p = n.buses_t.p.loc[snapshot]
    p_plus = p.clip(lower=0)
    p_minus = p.clip(upper=0)
    pp = p.to_frame().dot(p.to_frame().T).div(p).fillna(0)
    if downstream:
        indiag = diag(p_plus)
        offdiag = (p_minus.to_frame().dot(p_plus.to_frame().T)
                   .div(p_plus.sum()))
        pp = pp.clip(upper=0).add(diag(pp)).mul(np.sign(p.clip(lower=0)))
        bounds = pd.concat([pp.stack(), pp.stack().clip(lower=0)], axis=1,
                           keys=['lb', 'ub'])

#                   .pipe(lambda df: df - np.diagflat(np.diag(df)))
    else:
        indiag = diag(p_minus)
        offdiag = (p_plus.to_frame().dot(p_minus.to_frame().T)
                   .div(p_minus.sum()))
        pp = pp.clip(lower=0).add(diag(pp)).mul(-np.sign(p.clip(upper=0)))
        bounds = pd.concat([pp.stack().clip(upper=0), pp.stack()], axis=1,
                           keys=['lb', 'ub'])
    x0 = (indiag + offdiag).stack()
    N = len(n.buses)
    if method == 'min':
        sign = 1
    elif method == 'max':
        sign = -1

    def minimization(df):
        return sign * (H.dot(df.reshape(N, N)).stack()**2).sum()

    constr = [
            #   nodal balancing
            {'type': 'eq', 'fun': lambda df: df.reshape(N, N).sum(0)},
            #    total injection of colors
            {'type': 'eq', 'fun': lambda df: df.reshape(N, N).sum(1)-p.values}
            ]

    #   sources-sinks-fixation
    res = minimize(minimization, x0, constraints=constr,
                   bounds=bounds, options={'maxiter': 1000}, tol=1e-5,
                   method='SLSQP')
    print(res)
    sol = pd.DataFrame(res.x.reshape(N, N), columns=n.buses.index,
                       index=n.buses.index).round(10)
    if per_bus:
        return (sol[indiag.sum()==0].T
                .rename_axis('sink', axis=int(downstream))
                .rename_axis('source', axis=int(not downstream))
                .stack()[lambda ds:ds != 0])
    else:
        return H.dot(sol).round(8)


def zbus_transmission(n, snapshot=None):
    '''
    This allocation builds up on the method presented in [1]. However, we
    provide for non-linear power flow an additional DC-approximated
    modification, neglecting the series resistance r for lines.


    [1] A. J. Conejo, J. Contreras, D. A. Lima, and A. Padilha-Feltrin,
        “$Z_{\rm bus}$ Transmission Network Cost Allocation,” IEEE Transactions
        on Power Systems, vol. 22, no. 1, pp. 342–349, Feb. 2007.

    '''
    n.calculate_dependent_values()
    snapshot = n.snapshots[0] if snapshot is None else snapshot
    slackbus = n.buses[(n.buses_t.v_ang == 0).all()].index[0]


    # linearised method, start from linearised admittance matrix
    y = 1.j * admittance(n, branch_components=['Line'])
    K = incidence_matrix(n, branch_components=['Line'])

    Y = K @ diag(y) @ K.T  # Ybus matrix

    Z = pinv(Y)
    # set angle of slackbus to 0
    Z = Z.add(-Z.loc[slackbus])
    # DC-approximated S = P
    # S = n.buses_t.p.loc[[snapshot]].T
    V = n.buses.v_nom.to_frame(snapshot) * \
        (1 + 1.j * n.buses_t.v_ang.loc[[snapshot]].T).rename_axis('bus0')
    I = Y @ V
    assert all((I * V).apply(np.real)
                == network_injection(n, snapshots=snapshot).T)

    # -------------------------------------------------------------------------
    # nonlinear method start with full admittance matrix from pypsa
#    n.sub_networks.obj[0].calculate_Y()
    # Zbus matrix
#    Y = pd.DataFrame(n.sub_networks.obj[0].Y.todense(), buses, buses)
#    Z = pd.DataFrame(pinv(Y), buses, buses)
#    Z = Z.add(-Z.loc[slackbus])

    # -------------------------------------------------------------------------

    # y_sh = n.lines.set_index(['bus0', 'bus1']).eval('g_pu + 1.j * b_pu')

    A = (K * y).T @ Z # == diag(y) @ K.T @ Z == PTDF
         #+ Z.mul(y_sh, axis=0, level=0).set_axis(n.lines.index, inplace=False)
    A = A.applymap(np.real_if_close)
    branches = ['Line']
    f = pd.concat([n.pnl(b).p0.loc[snapshot] for b in branches], keys=branches)

    V_l_at = lambda bus: pd.concat([n.df(b)[bus].map(V[snapshot])
                                    / n.df(b)[bus].map(n.buses.v_nom ** 2)
                                    for b in branches], keys=branches)

    V_l = V_l_at('bus0').where(f > 0, V_l_at('bus1'))

    # q = PTDF(n) * p[snapshot]
    q = A.mul(V_l, axis=0)\
         .mul(I[snapshot]) \
         .applymap(np.real) \
         .stack() \
         .rename_axis(['component', 'branch_i', 'bus']) \
         .reorder_levels(['bus', 'component', 'branch_i'])\
         .sort_index()

    return pd.concat([q], keys=[snapshot], names=['snapshot'])


def with_and_without_transit(n, snapshots=None,
                             branch_components=['Line', 'Link']):
    if not n.links.empty:
        from pypsa.allocation import admittance, incidence_matrix, diag, pinv
        Y = pd.concat([admittance(n, branch_components, sn)
                       for sn in snapshots], axis=1,
                       keys=snapshots)
        def dynamic_subnetwork_PTDF(K, branches_i, snapshot):
            y = Y.loc[branches_i, snapshot]
            return diag(y) @ K.T @ pinv(K @ diag(y) @ K.T)


    def regional_with_and_withtout_flow(region):
        print(region, '\n')
        in_region_buses = n.buses.query('country == @region').index
        vicinity_buses = pd.Index(
                            pd.concat(
                            [n.branches()[lambda df:
                                df.bus0.map(n.buses.country) == region].bus1,
                             n.branches()[lambda df:
                                 df.bus1.map(n.buses.country) == region].bus0]))\
                            .difference(in_region_buses)
        buses_i = in_region_buses.union(vicinity_buses).drop_duplicates()


        region_branches = n.branches()[lambda df:
                            (df.bus0.map(n.buses.country) == region) |
                            (df.bus1.map(n.buses.country) == region)] \
                            .rename_axis(['component', 'branch_i'])
        branches_i = region_branches.index

        K = incidence_matrix(n, branch_components).loc[buses_i, branches_i]

        #create regional injection pattern with nodal injection at the border
        #accounting for the cross border flow
        f = pd.concat([n.pnl(c).p0.loc[snapshots].T for c in branch_components],
                      keys=branch_components, sort=True).reindex(branches_i)

        p = (K @ f)
        p.loc[in_region_buses] >> \
            network_injection(n, snapshots).loc[snapshots, in_region_buses].T

        #modified injection pattern without transition
        im = p.loc[vicinity_buses][lambda ds: ds > 0]
        ex = p.loc[vicinity_buses][lambda ds: ds < 0]

        largerImport_b = im.sum() > - ex.sum()
        scaleImport = (im.sum() + ex.sum()) / im.sum()
        scaleExport = (im.sum() + ex.sum()) / ex.sum()
        netImOrEx = (im * scaleImport).T\
                    .where(largerImport_b, (ex * scaleExport).T)
        p_wo = pd.concat([p.loc[in_region_buses], netImOrEx.T])\
                 .reindex(buses_i).fillna(0)

        if 'Link' not in f.index.unique('component'):
            y = admittance(n, ['Line'])[branches_i]
            H = diag(y) @ K.T @ pinv(K @ diag(y) @ K.T)
            f_wo = H @ p_wo
    #        f >> H @ p
        else:
            f_wo = pd.concat(
                    (dynamic_subnetwork_PTDF(K, branches_i, sn) @ p_wo[sn]
                        for sn in snapshots), axis=1, keys=snapshots)


        f, f_wo = f.T, f_wo.T
        loss_with = f ** 2 @ n.branches().loc[branches_i, 'r_pu'].fillna(0)
        loss_wo = f_wo ** 2 @ n.branches().loc[branches_i, 'r_pu'].fillna(0)
        loss = pd.concat([loss_with, loss_wo], axis=1, keys=['with', 'without'])
        flow = pd.concat([f, f_wo], axis=1, keys=['with', 'without'])
        return {'flow': flow, 'loss': loss}


def marginal_welfare_contribution(n, snapshots=None, formulation='kirchhoff',
                                  return_networks=False):
    import pyomo.environ as pe
    from .opf import (extract_optimisation_results,
                      define_passive_branch_flows_with_kirchhoff)
    def fmap(f, iterable):
        # mapper for inplace functions
        for x in iterable:
            f(x)

    def profit_by_gen(n):
        price_by_generator = (n.buses_t.marginal_price
                              .reindex(columns=n.generators.bus)
                              .set_axis(n.generators.index, axis=1,
                                        inplace=False))
        revenue = price_by_generator * n.generators_t.p
        cost = n.generators_t.p.multiply(n.generators.marginal_cost, axis=1)
        return ((revenue - cost).rename_axis('profit')
                .rename_axis('generator', axis=1))

    if snapshots is None:
        snapshots = n.snapshots
    n.lopf(snapshots, solver_name='gurobi_persistent', formulation=formulation)
    m = n.model

    networks = {}
    networks['orig_model'] = n if return_networks else profit_by_gen(n)

    m.zero_flow_con = pe.ConstraintList()

    for line in n.lines.index:
#        m.solutions.load_from(n.results)
        n_temp = n.copy()
        n_temp.model = m
        n_temp.mremove('Line', [line])

        # set line flow to zero
        line_var = m.passive_branch_p['Line', line, :]
        fmap(lambda ln: m.zero_flow_con.add(ln == 0), line_var)

        fmap(n.opt.add_constraint, m.zero_flow_con.values())

        # remove cycle constraint from persistent solver
        fmap(n.opt.remove_constraint, m.cycle_constraints.values())

        # remove cycle constraint from model
        fmap(m.del_component, [c for c in dir(m) if 'cycle_constr' in c])
        # add new cycle constraint to model
        define_passive_branch_flows_with_kirchhoff(n_temp, snapshots, True)
        # add cycle constraint to persistent solver
        fmap(n.opt.add_constraint, m.cycle_constraints.values())

        # solve
        n_temp.results = n.opt.solve()
        m.solutions.load_from(n_temp.results)

        # extract results
        extract_optimisation_results(n_temp, snapshots,
                                     formulation='kirchhoff')

        if not return_networks:
            n_temp = profit_by_gen(n_temp)
        networks[line] = n_temp

        # reset model
        fmap(n.opt.remove_constraint, m.zero_flow_con.values())
        m.zero_flow_con.clear()

    return (pd.Series(networks)
            .rename_axis('removed line')
            .rename('Network'))



def flow_allocation(n, snapshots=None, method='Average participation',
                    key=None, parallelized=False, nprocs=None, to_hdf=False,
                    **kwargs):
    """
    Function to allocate the total network flow to buses. Available
    methods are 'Average participation' ('ap'), 'Marginal
    participation' ('mp'), 'Virtual injection pattern' ('vip'),
    'Minimal flow shares' ('mfs').



    Parameters
    ----------

    network : pypsa.Network object

    snapshots : string or pandas.DatetimeIndex
                (subset of) snapshots of the network

    per_bus : Boolean, default is False
              Whether to allocate the flow in an peer-to-peeer manner,

    method : string
        Type of the allocation method. Should be one of

            - 'Average participation'/'ap':
                Trace the active power flow from source to sink
                (or sink to source) using the principle of proportional
                sharing and calculate the partial flows on each line,
                or to each bus where the power goes to (or comes from).
            - 'Marginal participation'/'mp':
                Allocate line flows according to linear sensitvities
                of nodal power injection given by the changes in the
                power transfer distribution factors (PTDF)
            - 'Virtual injection pattern'/'vip'
                Sequentially calculate the load flow induced by
                individual power sources in the network ignoring other
                sources and scaling down sinks.
            - 'Least square color flows'/'mfs'


    Returns
    -------
    res : dict
        The returned dict consists of two values of which the first,
        'flow', represents the allocated flows within a mulitindexed
        pandas.Series with levels ['snapshot', 'bus', 'line']. The
        second object, 'cost', returns the corresponding cost derived
        from the flow allocation.
    """
#    raise error if there are no flows

    snapshots = n.snapshots if snapshots is None else snapshots
    snapshots = snapshots if isinstance(snapshots, Iterable) else [snapshots]
    if n.lines_t.p0.shape[0] == 0:
        raise ValueError('Flows are not given by the network, '
                         'please solve the network flows first')
    n.calculate_dependent_values()

    if method in ['Average participation', 'ap']:
        method_func = average_participation
    elif method in ['Marginal Participation', 'mp']:
        method_func = marginal_participation
    elif method in ['Virtual injection pattern', 'vip']:
        method_func = virtual_injection_pattern
    elif method in ['Minimal flow shares', 'mfs']:
        method_func = minimal_flow_shares
    else:
        raise(ValueError('Method not implemented, please choose one out of'
                         "['Average participation', "
                         "'Marginal participation',"
                         "'Virtual injection pattern',"
                         "'Least square color flows']"))

    if snapshots is None:
        snapshots = n.snapshots
    if isinstance(snapshots, str):
        snapshots = [snapshots]

    if parallelized and not to_hdf:
        f = lambda sn: method_func(n, sn, **kwargs)
    else:
        def f(sn):
            if sn.is_month_start & (sn.hour == 0):
                logger.info('Allocating for %s %s'%(sn.month_name(), sn.year))
            return method_func(n, sn, **kwargs)


    if to_hdf:
        import random
        hash = random.getrandbits(12)
        store = '/tmp/temp{}.h5'.format(hash) if not isinstance(to_hdf, str) \
                else to_hdf
        periods = pd.period_range(snapshots[0], snapshots[-1], freq='m')
        p_str = lambda p: '_t_' + str(p).replace('-', '')
        for p in periods:
            p_slicer = snapshots.slice_indexer(p.start_time, p.end_time)
            gen = (f(sn) for sn in snapshots[p_slicer])
            pd.concat(gen).to_hdf(store, p_str(p))

        gen = (pd.read_hdf(store, p_str(p)).pipe(set_cats, n) for p in periods)
        flow = pd.concat(gen)
        os.remove(store)

    elif parallelized:
        flow = pd.concat(parmap(f, snapshots, nprocs=nprocs))
    else:
        flow = pd.concat((f(sn) for sn in snapshots))
    return flow.rename('allocation')


def chord_diagram(allocation, lower_bound=0, groups=None, size=300,
                  save_path='/tmp/chord_diagram_pypsa'):
    """
    This function builds a chord diagram on the base of holoviews [1].
    It visualizes allocated peer-to-peer flows for all buses given in
    the data. As for compatibility with ipython shell the rendering of
    the image is passed to matplotlib however to the disfavour of
    interactivity. Note that the plot becomes only meaningful for networks
    with N > 5, because of sparse flows otherwise.


    [1] http://holoviews.org/reference/elements/bokeh/Chord.html

    Parameters
    ----------

    allocation : pandas.Series (MultiIndex)
        Series of power transmission between buses. The first index
        level ('source') represents the source of the flow, the second
        level ('sink') its sink.
    lower_bound : int, default is 0
        filter small power flows by a lower bound
    groups : pd.Series, default is None
        Specify the groups of your buses, which are then used for coloring.
        The series must contain values for all allocated buses.
    size : int, default is 300
        Set the size of the holoview figure
    save_path : str, default is '/tmp/chord_diagram_pypsa'
        set the saving path of your figure

    """

    import holoviews as hv
    hv.extension('matplotlib')
    from IPython.display import Image

    if len(allocation.index.levels) == 3:
        allocation = allocation[allocation.index.levels[0][0]]

    allocated_buses = allocation.index.levels[0] \
                      .append(allocation.index.levels[1]).unique()
    bus_map = pd.Series(range(len(allocated_buses)), index=allocated_buses)

    links = allocation.to_frame('value').reset_index()\
        .replace({'source': bus_map, 'sink': bus_map})\
        .sort_values('source').reset_index(drop=True) \
        [lambda df: df.value >= lower_bound]

    nodes = pd.DataFrame({'bus': bus_map.index})
    if groups is None:
        cindex = 'index'
        ecindex = 'source'
    else:
        groups = groups.rename(index=bus_map)
        nodes = nodes.assign(groups=groups)
        links = links.assign(groups=links['source']
                             .map(groups))
        cindex = 'groups'
        ecindex = 'groups'

    nodes = hv.Dataset(nodes, 'index')
    diagram = hv.Chord((links, nodes))
    diagram = diagram.opts(style={'cmap': 'Category20',
                                  'edge_cmap': 'Category20'},
                           plot={'label_index': 'bus',
                                 'color_index': cindex,
                                 'edge_color_index': ecindex
                                 })
    renderer = hv.renderer('matplotlib').instance(fig='png', holomap='gif',
                                                  size=size, dpi=300)
    renderer.save(diagram, 'example_I')
    return Image(filename='example_I.png', width=800, height=800)



