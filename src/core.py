import math
import os
import sys
import logging
import time
import copy
import shutil
import os.path as op

import numpy as num

from pyrocko.guts import load, Object, String, Float, Int, Bool, List, \
    StringChoice, Dict
from pyrocko import orthodrome as od, gf, trace, guts, util, weeding
from pyrocko import parimap, model

from grond import dataset

logger = logging.getLogger('grond.core')

guts_prefix = 'grond'


def mahalanobis_distance(xs, mx, cov):
    imask = num.diag(cov) != 0.
    icov = num.linalg.inv(cov[imask, :][:, imask])
    temp = xs[:, imask] - mx[imask]
    return num.sqrt(num.sum(temp * num.dot(icov, temp.T).T, axis=1))


class Parameter(Object):
    name = String.T()
    unit = String.T(optional=True)
    scale_factor = Float.T(default=1., optional=True)
    scale_unit = String.T(optional=True)
    label = String.T(optional=True)

    def __init__(self, *args, **kwargs):
        if len(args) >= 1:
            kwargs['name'] = args[0]
        if len(args) >= 2:
            kwargs['unit'] = args[1]

        Object.__init__(self, **kwargs)

    def get_label(self, with_unit=True):
        l = [self.label or self.name]
        if with_unit:
            unit = self.get_unit_label()
            if unit:
                l.append('[%s]' % unit)

        return ' '.join(l)

    def get_value_label(self, value, format='%(value)g%(unit)s'):
        value = self.scaled(value)
        unit = self.get_unit_suffix()
        return format % dict(value=value, unit=unit)

    def get_unit_label(self):
        if self.scale_unit is not None:
            return self.scale_unit
        elif self.unit:
            return self.unit
        else:
            return None

    def get_unit_suffix(self):
        unit = self.get_unit_label()
        if not unit:
            return ''
        else:
            return ' %s' % unit

    def scaled(self, x):
        if isinstance(x, tuple):
            return tuple(v/self.scale_factor for v in x)
        if isinstance(x, list):
            return list(v/self.scale_factor for v in x)
        else:
            return x/self.scale_factor


class ADict(dict):
    def __getattr__(self, k):
        return self[k]

    def __setattr__(self, k, v):
        self[k] = v


class Problem(Object):
    name = String.T()
    parameters = List.T(Parameter.T())
    dependants = List.T(Parameter.T())
    apply_balancing_weights = Bool.T(default=True)

    def __init__(self, **kwargs):
        Object.__init__(self, **kwargs)
        self._bootstrap_weights = None
        self._target_weights = None
        self._engine = None

    def get_engine(self):
        return self._engine

    def copy(self):
        o = copy.copy(self)
        o._bootstrap_weights = None
        o._target_weights = None
        return o

    def parameter_dict(self, x):
        return ADict((p.name, v) for (p, v) in zip(self.parameters, x))

    def parameter_array(self, d):
        return num.array([d[p.name] for p in self.parameters], dtype=num.float)

    @property
    def parameter_names(self):
        return [p.name for p in self.combined]

    def dump_problem_info(self, dirname):
        fn = op.join(dirname, 'problem.yaml')
        util.ensuredirs(fn)
        guts.dump(self, filename=fn)

    def dump_problem_data(self, dirname, x, ms, ns):
        fn = op.join(dirname, 'x')
        with open(fn, 'ab') as f:
            x.astype('<f8').tofile(f)

        fn = op.join(dirname, 'misfits')
        with open(fn, 'ab') as f:
            ms.astype('<f8').tofile(f)
            ns.astype('<f8').tofile(f)

    def name_to_index(self, name):
        pnames = [p.name for p in self.combined]
        return pnames.index(name)

    @property
    def nparameters(self):
        return len(self.parameters)

    @property
    def ntargets(self):
        return len(self.targets)

    @property
    def ndependants(self):
        return len(self.dependants)

    @property
    def ncombined(self):
        return len(self.parameters) + len(self.dependants)

    @property
    def combined(self):
        return self.parameters + self.dependants

    def make_bootstrap_weights(self, nbootstrap):
        ntargets = len(self.targets)
        ws = num.zeros((nbootstrap, ntargets))
        rstate = num.random.RandomState(23)
        for ibootstrap in xrange(nbootstrap):
            ii = rstate.randint(0, ntargets, size=self.ntargets)
            ws[ibootstrap, :] = num.histogram(
                ii, ntargets, (-0.5, ntargets - 0.5))[0]

        return ws

    def get_bootstrap_weights(self, ibootstrap=None):
        if self._bootstrap_weights is None:
            self._bootstrap_weights = self.make_bootstrap_weights(
                self.nbootstrap)

        if ibootstrap is None:
            return self._bootstrap_weights
        else:
            return self._bootstrap_weights[ibootstrap, :]

    def set_engine(self, engine):
        self._engine = engine


class ProblemConfig(Object):
    name_template = String.T()
    apply_balancing_weights = Bool.T(default=True)


class Forbidden(Exception):
    pass


class DirectoryAlreadyExists(Exception):
    pass


class GrondError(Exception):
    pass


class InnerMisfitConfig(Object):
    fmin = Float.T()
    fmax = Float.T()
    ffactor = Float.T(default=1.5)
    tmin = gf.Timing.T()
    tmax = gf.Timing.T()
    pick_synthetic_traveltime = gf.Timing.T(optional=True)
    pick_phasename = String.T(optional=True)
    domain = trace.DomainChoice.T(default='time_domain')


class TargetAnalysisResult(Object):
    balancing_weight = Float.T()


class NoAnalysisResults(Exception):
    pass


class MisfitTarget(gf.Target):
    misfit_config = InnerMisfitConfig.T()
    flip_norm = Bool.T(default=False)
    manual_weight = Float.T(default=1.0)
    analysis_result = TargetAnalysisResult.T(optional=True)
    groupname = gf.StringID.T(optional=True)

    def __init__(self, **kwargs):
        gf.Target.__init__(self, **kwargs)
        self._ds = None
        self._return_traces = False

    def get_plain_target(self):
        d = dict(
            (k, getattr(self, k)) for k in gf.Target.T.propnames)
        return gf.Target(**d)

    def get_dataset(self):
        return self._ds

    def set_dataset(self, ds):
        self._ds = ds

    def set_return_traces(self, return_traces):
        self._return_traces = return_traces

    def get_combined_weight(self, apply_balancing_weights):
        w = self.manual_weight
        if apply_balancing_weights:
            w *= self.get_balancing_weight()

        return w

    def get_balancing_weight(self):
        if not self.analysis_result:
            raise NoAnalysisResults('no balancing weights available')

        return self.analysis_result.balancing_weight

    def get_taper_params(self, engine, source):
        store = engine.get_store(self.store_id)
        config = self.misfit_config
        tmin_fit = source.time + store.t(config.tmin, source, self)
        tmax_fit = source.time + store.t(config.tmax, source, self)
        tfade = 1.0/config.fmin
        return tmin_fit, tmax_fit, tfade

    def post_process(self, engine, source, tr_syn):

        tr_syn = tr_syn.pyrocko_trace()
        print tr_syn.deltat
        nslc = self.codes

        config = self.misfit_config

        tmin_fit, tmax_fit, tfade = self.get_taper_params(engine, source)

        ds = self.get_dataset()

        tobs_shift = 0.0
        if config.pick_synthetic_traveltime and config.pick_phasename:
            store = engine.get_store(self.store_id)
            tsyn = source.time + store.t(
                config.pick_synthetic_traveltime, source, self)

            marker = ds.get_pick(
                source.name,
                self.codes[:3],
                config.pick_phasename)

            if marker:
                tobs = marker.tmin
                tobs_shift = tobs - tsyn

        print tobs_shift

        freqlimits = (
            config.fmin/config.ffactor,
            config.fmin, config.fmax,
            config.fmax*config.ffactor)

        tinc_obs = 1.0/config.fmin

        tr_syn.extend(
            tmin_fit - tfade * 2.0,
            tmax_fit + tfade * 2.0,
            fillmethod='repeat')

        tr_syn = tr_syn.transfer(
            freqlimits=freqlimits,
            tfade=tfade)

        tr_syn.chop(tmin_fit - 2*tfade, tmax_fit + 2*tfade)

        tmin_obs = (math.floor((tmin_fit - tfade + tobs_shift) / tinc_obs) - 1.0) * tinc_obs
        tmax_obs = (math.ceil((tmax_fit + tfade + tobs_shift) / tinc_obs) + 1.0) * tinc_obs


        try:
            if nslc[-1] == 'R':
                backazimuth = self.azimuth + 180.
            elif nslc[-1] == 'T':
                backazimuth = self.azimuth + 90.
            else:
                backazimuth = None

            tr_obs = ds.get_waveform(
                nslc,
                tmin=tmin_obs,
                tmax=tmax_obs,
                tfade=tfade,
                freqlimits=freqlimits,
                deltat=tr_syn.deltat,
                cache=True,
                backazimuth=backazimuth)

            if tobs_shift != 0.0:
                tr_obs = tr_obs.copy()
                tr_obs.shift(-tobs_shift)

            ms = trace.MisfitSetup(
                norm=2,
                domain=config.domain,
                filter=trace.PoleZeroResponse(),
                taper=trace.CosTaper(
                    tmin_fit - tfade,
                    tmin_fit,
                    tmax_fit,
                    tmax_fit + tfade))

            if not self.flip_norm:
                mv, mn, tr_obs_proc, tr_syn_proc = tr_obs.misfit(
                    tr_syn, ms, nocache=True, debug=True)
            else:
                mv, mn, tr_syn_proc, tr_obs_proc = tr_syn.misfit(
                    tr_obs, ms, nocache=True, debug=True)

            result = MisfitResult(misfit_value=mv, misfit_norm=mn)
            if self._return_traces:
                result.filtered_obs = tr_obs
                result.filtered_syn = tr_syn
                result.processed_obs = tr_obs_proc
                result.processed_syn = tr_syn_proc
                result.taper = ms.taper

            return result

        except dataset.NotFound, e:
            logger.debug(str(e))
            raise gf.SeismosizerError('no waveform data, %s' % str(e))


class Trace(Object):
    pass


class MisfitResult(gf.Result):
    misfit_value = Float.T()
    misfit_norm = Float.T()
    processed_obs = Trace.T(optional=True)
    processed_syn = Trace.T(optional=True)
    filtered_obs = Trace.T(optional=True)
    filtered_syn = Trace.T(optional=True)
    taper = trace.Taper.T(optional=True)


def xjoin(basepath, path):
    if path is None and basepath is not None:
        return basepath
    elif op.isabs(path) or basepath is None:
        return path
    else:
        return op.join(basepath, path)


def xrelpath(path, start):
    if op.isabs(path):
        return path
    else:
        return op.relpath(path, start)


class Path(String):
    pass


class HasPaths(Object):
    path_prefix = Path.T(optional=True)

    def __init__(self, *args, **kwargs):
        Object.__init__(self, *args, **kwargs)
        self._basepath = None
        self._parent_path_prefix = None

    def set_basepath(self, basepath, parent_path_prefix=None):
        self._basepath = basepath
        self._parent_path_prefix = parent_path_prefix
        for (prop, val) in self.T.ipropvals(self):
            if isinstance(val, HasPaths):
                val.set_basepath(
                    basepath, self.path_prefix or self._parent_path_prefix)

    def get_basepath(self):
        assert self._basepath is not None
        return self._basepath

    def change_basepath(self, new_basepath, parent_path_prefix=None):
        assert self._basepath is not None

        self._parent_path_prefix = parent_path_prefix
        if self.path_prefix or not self._parent_path_prefix:

            self.path_prefix = op.normpath(xjoin(xrelpath(
                self._basepath, new_basepath), self.path_prefix))

        for val in self.T.ivals(self):
            if isinstance(val, HasPaths):
                val.change_basepath(
                    new_basepath, self.path_prefix or self._parent_path_prefix)

        self._basepath = new_basepath

    def expand_path(self, path):
        assert self._basepath is not None

        path_prefix = self.path_prefix or self._parent_path_prefix

        if path is None:
            return None
        elif isinstance(path, basestring):
            return op.normpath(xjoin(self._basepath, xjoin(path_prefix, path)))
        else:
            return [
                op.normpath(xjoin(self._basepath, xjoin(path_prefix, p)))
                for p in path]


class SyntheticTest(Object):
    random_seed = Int.T(default=0)
    inject_solution = Bool.T(default=False)
    x = Dict.T(String.T(), Float.T())

    def __init__(self, **kwargs):
        Object.__init__(self, **kwargs)
        self._synthetics = None

    def set_config(self, config):
        self._config = config

    def get_problem(self):
        ds = self._config.get_dataset()
        events = ds.get_events()
        rstate = num.random.RandomState(self.random_seed)
        event = events[rstate.randint(0, len(events))]
        return self._config.get_problem(event)

    def get_x_random(self):
        problem = self.get_problem()
        xbounds = num.array(problem.bounds(), dtype=num.float)
        npar = xbounds.shape[0]

        rstate = num.random.RandomState(self.random_seed)
        x = num.zeros(npar, dtype=num.float)
        while True:
            for i in xrange(npar):
                x[i] = rstate.uniform(xbounds[i, 0], xbounds[i, 1])

            try:
                x = problem.preconstrain(x)
                break

            except Forbidden:
                pass

        return x

    def get_x(self):
        problem = self.get_problem()
        if self.x:
            x = problem.preconstrain(
                problem.parameter_array(self.x))

        else:
            x = self.get_x_random()

        return x

    def get_synthetics(self):
        if self._synthetics is None:
            problem = self.get_problem()

            x = self.get_x()

            results = problem.forward(x)
            self._synthetics = results

        return self._synthetics

    def get_waveform(self, nslc, tmin, tmax, tfade=0., freqlimits=None):
        synthetics = self.get_synthetics()
        for result in synthetics:
            if result.trace.codes == nslc:
                tr = result.trace.pyrocko_trace()
                tr.extend(tmin - tfade * 2.0, tmax + tfade * 2.0)
                tr = tr.transfer(tfade=tfade, freqlimits=freqlimits)
                tr.chop(tmin, tmax)
                return tr


class DatasetConfig(HasPaths):

    stations_path = Path.T(optional=True)
    stations_stationxml_paths = List.T(Path.T())
    events_path = Path.T()
    waveform_paths = List.T(Path.T())
    clippings_path = Path.T(optional=True)
    responses_sacpz_path = Path.T(optional=True)
    responses_stationxml_paths = List.T(Path.T())
    station_corrections_path = Path.T(optional=True)
    apply_correction_factors = Bool.T(default=True)
    apply_correction_delays = Bool.T(default=True)
    picks_paths = List.T(Path.T())
    blacklist = List.T(String.T())
    whitelist = List.T(String.T(), optional=True)
    synthetic_test = SyntheticTest.T(optional=True)

    def __init__(self, *args, **kwargs):
        HasPaths.__init__(self, *args, **kwargs)
        self._ds = None

    def get_dataset(self):
        if self._ds is None:
            fp = self.expand_path
            ds = dataset.Dataset()
            ds.add_stations(
                pyrocko_stations_filename=fp(self.stations_path),
                stationxml_filenames=fp(self.stations_stationxml_paths))

            ds.add_events(filename=fp(self.events_path))
            ds.add_waveforms(paths=fp(self.waveform_paths))
            if self.clippings_path:
                ds.add_clippings(markers_filename=fp(self.clippings_path))

            if self.responses_sacpz_path:
                ds.add_responses(
                    sacpz_dirname=fp(self.responses_sacpz_path))

            if self.responses_stationxml_paths:
                ds.add_responses(
                    stationxml_filenames=fp(self.responses_stationxml_paths))

            if self.station_corrections_path:
                ds.add_station_corrections(
                    filename=fp(self.station_corrections_path))

            ds.apply_correction_factors = self.apply_correction_factors
            ds.apply_correction_delays = self.apply_correction_delays

            for picks_path in self.picks_paths:
                ds.add_picks(
                    filename=fp(picks_path))

            ds.add_blacklist(self.blacklist)
            if self.whitelist:
                ds.add_whitelist(self.whitelist)

            ds.set_synthetic_test(self.synthetic_test)
            self._ds = ds

        return self._ds


def weed(origin, targets, limit, neighborhood=3):

    azimuths = num.zeros(len(targets))
    dists = num.zeros(len(targets))
    for i, target in enumerate(targets):
        _, azimuths[i] = target.azibazi_to(origin)
        dists[i] = target.distance_to(origin)

    badnesses = num.ones(len(targets), dtype=float)
    deleted, meandists_kept = weeding.weed(
        azimuths, dists, badnesses,
        nwanted=limit,
        neighborhood=neighborhood)

    targets_weeded = [
        target for (delete, target) in zip(deleted, targets) if not delete]

    return targets_weeded, meandists_kept, deleted


class TargetConfig(Object):

    groupname = gf.StringID.T(optional=True)
    distance_min = Float.T(optional=True)
    distance_max = Float.T(optional=True)
    limit = Int.T(optional=True)
    channels = List.T(String.T())
    inner_misfit_config = InnerMisfitConfig.T()
    interpolation = gf.InterpolationMethod.T()
    store_id = gf.StringID.T()
    weight = Float.T(default=1.0)

    def get_targets(self, ds, event, default_groupname):

        origin = event

        targets = []
        for st in ds.get_stations():
            for cha in self.channels:
                target = MisfitTarget(
                    quantity='displacement',
                    codes=st.nsl() + (cha,),
                    lat=st.lat,
                    lon=st.lon,
                    interpolation=self.interpolation,
                    store_id=self.store_id,
                    misfit_config=self.inner_misfit_config,
                    manual_weight=self.weight,
                    groupname=self.groupname or default_groupname)

                if self.distance_min is not None and \
                        target.distance_to(origin) < self.distance_min:
                    continue

                if self.distance_max is not None and \
                        target.distance_to(origin) > self.distance_max:
                    continue

                azi, _ = target.azibazi_to(origin)
                if cha == 'R':
                    target.azimuth = azi - 180.
                    target.dip = 0.
                elif cha == 'T':
                    target.azimuth = azi - 90.
                    target.dip = 0.
                elif cha == 'Z':
                    target.azimuth = 0.
                    target.dip = -90.

                target.set_dataset(ds)
                targets.append(target)

        if self.limit:
            return weed(origin, targets, self.limit)[0]
        else:
            return targets


class AnalyserConfig(Object):
    niter = Int.T(default=1000)


class SamplerDistributionChoice(StringChoice):
    choices = ['multivariate_normal', 'normal']


class SolverConfig(Object):
    niter_uniform = Int.T(default=1000)
    niter_transition = Int.T(default=0)
    niter_explorative = Int.T(default=10000)
    niter_non_explorative = Int.T(default=0)
    sampler_distribution = SamplerDistributionChoice.T(
        default='multivariate_normal')

    def get_solver_kwargs(self):
        return dict(
            niter_uniform=self.niter_uniform,
            niter_transition=self.niter_transition,
            niter_explorative=self.niter_explorative,
            niter_non_explorative=self.niter_non_explorative,
            sampler_distribution=self.sampler_distribution)


class EngineConfig(HasPaths):
    gf_stores_from_pyrocko_config = Bool.T(default=True)
    gf_store_superdirs = List.T(Path.T())
    gf_store_dirs = List.T(Path.T())

    def __init__(self, *args, **kwargs):
        HasPaths.__init__(self, *args, **kwargs)
        self._engine = None

    def get_engine(self):
        if self._engine is None:
            fp = self.expand_path
            self._engine = gf.LocalEngine(
                use_config=self.gf_stores_from_pyrocko_config,
                store_superdirs=fp(self.gf_store_superdirs),
                store_dirs=fp(self.gf_store_dirs))

        return self._engine


class Config(HasPaths):
    rundir_template = Path.T()
    dataset_config = DatasetConfig.T()
    target_configs = List.T(TargetConfig.T())
    problem_config = ProblemConfig.T()
    niter_analyse_problem = Int.T(default=1000)
    analyser_config = AnalyserConfig.T(default=AnalyserConfig.D())
    solver_config = SolverConfig.T(default=SolverConfig.D())
    engine_config = EngineConfig.T(default=EngineConfig.D())

    def __init__(self, *args, **kwargs):
        HasPaths.__init__(self, *args, **kwargs)

    def get_dataset(self):
        ds = self.dataset_config.get_dataset()
        if ds.synthetic_test:
            ds.synthetic_test.set_config(self)

        return ds

    def get_targets(self, event):
        ds = self.get_dataset()

        targets = []
        for igroup, target_config in enumerate(self.target_configs):
            targets.extend(target_config.get_targets(
                ds, event, 'group_%i' % igroup))

        return targets

    def get_problem(self, event):
        targets = self.get_targets(event)
        problem = self.problem_config.get_problem(event, targets)
        problem.set_engine(self.engine_config.get_engine())
        return problem


def sarr(a):
    return ' '.join('%15g' % x for x in a)


def load_problem_info_and_data(dirname, subset=None):
    problem = load_problem_info(dirname)
    xs, misfits = load_problem_data(xjoin(dirname, subset), problem)
    return problem, xs, misfits


def load_problem_info(dirname):
    fn = op.join(dirname, 'problem.yaml')
    return guts.load(filename=fn)


def load_problem_data(dirname, problem):
    fn = op.join(dirname, 'x')
    with open(fn, 'r') as f:
        nmodels = os.fstat(f.fileno()).st_size / (problem.nparameters * 8)
        data = num.fromfile(
            f, dtype='<f8',
            count=nmodels*problem.nparameters).astype(num.float)

    nmodels = data.size/problem.nparameters
    xs = data.reshape((nmodels, problem.nparameters))

    fn = op.join(dirname, 'misfits')
    with open(fn, 'r') as f:
        data = num.fromfile(
            f, dtype='<f8', count=nmodels*problem.ntargets*2).astype(num.float)

    data = data.reshape((nmodels, problem.ntargets*2))

    combi = num.empty_like(data)
    combi[:, 0::2] = data[:, :problem.ntargets]
    combi[:, 1::2] = data[:, problem.ntargets:]

    misfits = combi.reshape((nmodels, problem.ntargets, 2))

    return xs, misfits


def get_mean_x(xs):
    return num.mean(xs, axis=0)


def get_best_x(problem, xs, misfits):
    gms = problem.global_misfits(misfits)
    ibest = num.argmin(gms)
    return xs[ibest, :]


def get_mean_source(problem, xs):
    x_mean = get_mean_x(xs)
    source = problem.unpack(x_mean)
    return source


def get_best_source(problem, xs, misfits):
    x_best = get_best_x(problem, xs, misfits)
    source = problem.unpack(x_best)
    return source


def mean_latlondist(lats, lons):
    if len(lats) == 0:
        return 0., 0., 1000.
    else:
        ns, es = od.latlon_to_ne_numpy(lats[0], lons[0], lats, lons)
        n, e = num.mean(ns), num.mean(es)
        dists = num.sqrt((ns-n)**2 + (es-e)**2)
        lat, lon = od.ne_to_latlon(lats[0], lons[0], n, e)
        return float(lat), float(lon), float(num.max(dists))


def stations_mean_latlondist(stations):
    lats = num.array([s.lat for s in stations])
    lons = num.array([s.lon for s in stations])
    return mean_latlondist(lats, lons)


def read_config(path):
    config = load(filename=path)
    config.set_basepath(op.dirname(path) or '.')
    return config


def analyse(problem, niter=1000, show_progress=False):
    if niter == 0:
        return

    wtargets = []
    for target in problem.targets:
        wtarget = copy.copy(target)
        wtarget.flip_norm = True
        wtarget.weight = 1.0
        wtargets.append(wtarget)

    wproblem = problem.copy()
    wproblem.targets = wtargets

    xbounds = num.array(wproblem.bounds(), dtype=num.float)
    npar = xbounds.shape[0]

    mss = num.zeros((niter, problem.ntargets))
    rstate = num.random.RandomState(123)

    if show_progress:
        pbar = util.progressbar('analysing problem', niter)

    for iiter in xrange(niter):
        while True:
            x = []
            for i in xrange(npar):
                v = rstate.uniform(xbounds[i, 0], xbounds[i, 1])
                x.append(v)

            try:
                x = wproblem.preconstrain(x)
                break

            except Forbidden:
                pass

        _, ms = wproblem.evaluate(x)
        mss[iiter, :] = ms

        if show_progress:
            pbar.update(iiter)

    if show_progress:
        pbar.finish()

    mean_ms = num.mean(mss, axis=0)

    weights = 1.0 / mean_ms
    weights /= (num.nansum(weights)/num.nansum(num.isfinite(weights)))

    for weight, target in zip(weights, problem.targets):
        target.analysis_result = TargetAnalysisResult(
            balancing_weight=float(weight))


def solve(problem,
          rundir=None,
          niter_uniform=1000,
          niter_transition=1000,
          niter_explorative=10000,
          niter_non_explorative=0,
          xs_inject=None,
          sampler_distribution='multivariate_normal',
          status=()):

    xbounds = num.array(problem.bounds(), dtype=num.float)
    npar = xbounds.shape[0]

    nlinks_cap = 8 * npar + 1
    chains_m = num.zeros((1 + problem.nbootstrap, nlinks_cap), num.float)
    chains_i = num.zeros((1 + problem.nbootstrap, nlinks_cap), num.int)
    nlinks = 0
    mbx = None

    if xs_inject is not None and xs_inject.size != 0:
        niter_inject = xs_inject.shape[0]
    else:
        niter_inject = 0

    niter = niter_inject + niter_uniform + niter_explorative + \
        niter_non_explorative

    iiter = 0
    sbx = None
    mxs = None
    covs = None
    xhist = num.zeros((niter, npar))
    isbad_mask = None
    accept_sum = num.zeros(1 + problem.nbootstrap, dtype=num.int)
    accept_hist = num.zeros(niter, dtype=num.int)

    while iiter < niter:

        if niter_inject + niter_uniform <= iiter and \
                iiter < niter_inject + niter_uniform + niter_transition:

            factor = 4.0 * (1.0 - (iiter - niter_uniform - niter_inject) /
                            float(niter_transition))
        else:
            factor = 1.0

        ntries_preconstrain = 0
        ntries_sample = 0

        if iiter < niter_inject:
            x = xs_inject[iiter, :]
        else:
            while True:
                ntries_preconstrain += 1

                if mbx is None or iiter < niter_inject + niter_uniform:
                    x = problem.random_uniform(xbounds)
                else:
                    # jchoice = num.random.randint(0, 1 + problem.nbootstrap)
                    jchoice = num.argmin(accept_sum)

                    if iiter < niter_inject + niter_uniform + \
                            niter_transition + niter_explorative:

                        ichoice = num.random.randint(0, nlinks)
                        xb = xhist[chains_i[jchoice, ichoice]]
                    else:
                        xb = mxs[jchoice]

                    if sampler_distribution == 'multivariate_normal':
                        ntries_sample = 0

                        while True:
                            ntries_sample += 1
                            vs = num.random.multivariate_normal(
                                xb, factor*covs[jchoice])

                            if (num.all(xbounds[:, 0] <= vs) and
                                    num.all(vs <= xbounds[:, 1])):
                                break

                        x = vs.tolist()

                    if sampler_distribution == 'normal':
                        for i in xrange(npar):
                            while True:
                                v = num.random.normal(
                                    xb[i], math.sqrt(factor)*sbx[i])

                                if xbounds[i, 0] <= v and v <= xbounds[i, 1]:
                                    break

                            x.append(v)

                try:
                    x = problem.preconstrain(x)
                    break

                except Forbidden:
                    pass

        ms, ns = problem.evaluate(x)

        isbad_mask_new = num.isnan(ms)
        if isbad_mask is not None and num.any(isbad_mask != isbad_mask_new):
            logger.error(
                'skipping problem %s: inconsistency in data availability' %
                problem.name)
            return

        isbad_mask = isbad_mask_new

        if num.all(isbad_mask):
            logger.error(
                'skipping problem %s: all target misfit values are NaN' %
                problem.name)
            return

        if rundir:
            problem.dump_problem_data(rundir, x, ms, ns)

        m = problem.global_misfit(ms, ns)
        ms = problem.bootstrap_misfit(ms, ns)

        chains_m[0, nlinks] = m
        chains_m[1:, nlinks] = ms
        chains_i[:, nlinks] = iiter

        nlinks += 1

        for ichain in xrange(chains_m.shape[0]):
            isort = num.argsort(chains_m[ichain, :nlinks])
            chains_m[ichain, :nlinks] = chains_m[ichain, isort]
            chains_i[ichain, :nlinks] = chains_i[ichain, isort]

        if nlinks == nlinks_cap:
            accept = (chains_i[:, nlinks_cap-1] != iiter).astype(num.int)
            nlinks -= 1
        else:
            accept = num.ones(1 + problem.nbootstrap, dtype=num.int)

        accept_sum += accept
        accept_hist[iiter] = num.sum(accept)

        lines = []
        if 'state' in status:
            lines.append('%i' % iiter)
            lines.append(''.join('-X'[int(acc)] for acc in accept))

        xhist[iiter, :] = x

        bxs = xhist[chains_i[:, :nlinks].ravel(), :]
        gxs = xhist[chains_i[0, :nlinks], :]
        gms = chains_m[0, :nlinks]

        if nlinks > (nlinks_cap-1)/2:
            # mean and std of all bootstrap ensembles together
            mbx = num.mean(bxs, axis=0)
            sbx = num.std(bxs, axis=0)

            # mean and std of global configuration
            mgx = num.mean(gxs, axis=0)
            sgx = num.std(gxs, axis=0)

            # best in global configuration
            bgx = xhist[chains_i[0, 0], :]

            covs = []
            mxs = []
            for i in xrange(1 + problem.nbootstrap):
                xs = xhist[chains_i[i, :nlinks], :]
                mx = num.mean(xs, axis=0)
                cov = num.cov(xs.T)

                mxs.append(mx)
                covs.append(cov)

            if 'state' in status:
                lines.append(
                    '%-15s %15s %15s %15s %15s %15s' %
                    ('parameter', 'B mean', 'B std', 'G mean', 'G std',
                     'G best'))

                for (pname, mbv, sbv, mgv, sgv, bgv) in zip(
                        [p.name for p in problem.parameters],
                        mbx, sbx, mgx, sgx, bgx):

                    lines.append(
                        '%-15s %15.4g %15.4g %15.4g %15.4g %15.4g' %
                        (pname, mbv, sbv, mgv, sgv, bgv))

                lines.append('%-15s %15s %15s %15.4g %15.4g %15.4g' % (
                    'misfit', '', '',
                    num.mean(gms), num.std(gms), num.min(gms)))

        if 'state' in status:
            if iiter < niter_inject:
                phase = 'inject'
            elif iiter < niter_inject + niter_uniform:
                phase = 'uniform'
            elif iiter < niter_inject + niter_uniform + niter_transition:
                phase = 'transition'
            elif iiter < niter_inject + niter_uniform + niter_transition + \
                    niter_explorative:
                phase = 'explorative'
            else:
                phase = 'non-explorative'

            lines.append(
                '%-15s %15i %-15s %15i %15i' % (
                    'iteration', iiter+1, '(%s)' % phase,
                    ntries_sample, ntries_preconstrain))

        if 'matrix' in status:
            matrix = (chains_i[:, :30] % 94 + 32).T
            for row in matrix[::-1]:
                lines.append(''.join(chr(xxx) for xxx in row))

        if status:
            lines[0:0] = ['\033[2J']
            lines.append('')
            print '\n'.join(lines)

        iiter += 1


def bootstrap_outliers(problem, misfits, std_factor=1.0):
    '''
    Identify bootstrap configurations performing bad in global configuration
    '''

    gms = problem.global_misfits(misfits)

    ibests = []
    for ibootstrap in xrange(problem.nbootstrap):
        bms = problem.bootstrap_misfits(misfits, ibootstrap)
        ibests.append(num.argmin(bms))

    m = num.median(gms[ibests])
    s = num.std(gms[ibests])

    return num.where(gms > m+s)[0]


def forward(rundir):

    # config = guts.load(filename=op.join('.', 'grond_td.conf'))
    # config.set_basepath('.')

    config = guts.load(filename=op.join(rundir, 'config.yaml'))
    config.set_basepath(rundir)
    ds = config.get_dataset()

    problem, xs, misfits = load_problem_info_and_data(rundir, subset='harvest')
    for target in problem.targets:
        target.set_dataset(ds)

    gms = problem.global_misfits(misfits)
    isort = num.argsort(gms)
    gms = gms[isort]
    xs = xs[isort, :]

    all_trs = []
    print gms[0]
    for xbest in xs[:1, :]:
        ms, ns, results = problem.evaluate(xbest, return_traces=True)
        print problem.global_misfit(ms, ns)

        for result in results:
            if result:
                result.filtered_obs.set_codes(location='ob')
                result.filtered_syn.set_codes(location='sy')
                all_trs.append(result.filtered_obs)
                all_trs.append(result.filtered_syn)

    trace.snuffle(all_trs)


def harvest(rundir, problem=None, nbest=10, force=False, weed=0):

    if problem is None:
        problem, xs, misfits = load_problem_info_and_data(rundir)
    else:
        xs, misfits = load_problem_data(rundir, problem)

    dumpdir = op.join(rundir, 'harvest')
    if op.exists(dumpdir):
        if force:
            shutil.rmtree(dumpdir)
        else:
            raise DirectoryAlreadyExists(dumpdir)

    util.ensuredir(dumpdir)

    ibests_list = []
    ibests = []
    for ibootstrap in xrange(problem.nbootstrap):
        bms = problem.bootstrap_misfits(misfits, ibootstrap)
        isort = num.argsort(bms)
        ibests_list.append(isort[:nbest])
        ibests.append(isort[0])

    gms = problem.global_misfits(misfits)
    isort = num.argsort(gms)

    ibests_list.append(isort[:nbest])

    if weed:
        mean_gm_best = num.median(gms[ibests])
        std_gm_best = num.std(gms[ibests])
        ibad = set()

        for ibootstrap, ibest in enumerate(ibests):
            if gms[ibest] > mean_gm_best + std_gm_best:
                ibad.add(ibootstrap)

        ibests_list = [
            ibests_ for (ibootstrap, ibests_) in enumerate(ibests_list)
            if ibootstrap not in ibad]

    ibests = num.concatenate(ibests_list)

    if weed == 2:
        ibests = ibests[gms[ibests] < mean_gm_best]

    for i in ibests:
        x = xs[i]
        ms = misfits[i, :, 0]
        ns = misfits[i, :, 1]
        problem.dump_problem_data(dumpdir, x, ms, ns)


def check_problem(problem):
    if len(problem.targets) == 0:
        raise GrondError('no targets available')


g_state = {}

def check(config):
    ds = config.get_dataset()
    events = ds.get_events()
    nevents = len(events)

    from matplotlib import pyplot as plt

    if nevents == 0:
        raise GrondError('no events found')

    for ievent, event in enumerate(events):
        try:
            all_trs = []
            problem = config.get_problem(event)
            check_problem(problem)

            xbounds = num.array(problem.bounds(), dtype=num.float)

            results_list = []
            for i in xrange(10):
                x = problem.random_uniform(xbounds)
                ms, ns, results = problem.evaluate(x, return_traces=True)
                results_list.append(results)


            for itarget, target in enumerate(problem.targets):

                yabsmaxs = []
                for results in results_list:
                    result = results[itarget]
                    if result:
                        yabsmaxs.append(
                            num.max(num.abs(result.filtered_obs.get_ydata())))

                if yabsmaxs:
                    yabsmax = max(yabsmaxs) or 1.0
                else:
                    yabsmax = None

                fig = None
                for results in results_list:
                    result = results[itarget]
                    if result:
                        if fig is None:
                            fig = plt.figure()
                            axes = fig.add_subplot(1, 1, 1)
                            axes.set_ylim(0., 4.)
                            axes.set_title('%s %s' % (
                                '.'.join(x for x in target.codes if x),
                                target.groupname))

                        xdata = result.filtered_obs.get_xdata()
                        ydata = result.filtered_obs.get_ydata() / yabsmax
                        axes.plot(xdata, ydata*0.5 + 3.5, color='black')

                        xdata = result.filtered_syn.get_xdata()
                        ydata = result.filtered_syn.get_ydata()
                        ydata = ydata / (num.max(num.abs(ydata)) or 1.0)

                        axes.plot(xdata, ydata*0.5 + 2.5, color='red')

                        xdata = result.processed_syn.get_xdata()
                        ydata = result.processed_syn.get_ydata()
                        ydata = ydata / (num.max(num.abs(ydata)) or 1.0)

                        axes.plot(xdata, ydata*0.5 + 1.5, color='red')

                        t = result.processed_syn.get_xdata()
                        taper = result.taper

                        y = num.ones(t.size) * 0.9
                        taper(y, t[0], t[1] - t[0])
                        y2 = num.concatenate((y, -y[::-1]))
                        t2 = num.concatenate((t, t[::-1]))
                        axes.plot(t2, y2 * 0.5 + 0.5, color='gray')

                if fig:
                    plt.show()

        except GrondError, e:
            logger.error('event %i, %s: %s' % (
                ievent,
                event.name or util.time_to_str(event.time),
                str(e)))



def go(config, force=False, nparallel=1, status=('state',)):

    status = tuple(status)

    ds = config.get_dataset()
    events = ds.get_events()
    nevents = len(events)

    if nevents == 0:
        raise GrondError('no events found')

    g_data = (config, force, status, nparallel)

    g_state[id(g_data)] = g_data

    for x in parimap.parimap(
            process_event,
            xrange(nevents),
            [id(g_data)] * nevents,
            nprocs=nparallel):

        pass


def process_event(ievent, g_data_id):

    config, force, status, nparallel = g_state[g_data_id]

    if nparallel > 1:
        status = ()

    ds = config.get_dataset()

    events = ds.get_events()
    nevents = len(events)

    event = events[ievent]

    ds.empty_cache()

    tstart = time.time()

    problem = config.get_problem(event)

    # FIXME
    synt = ds.synthetic_test
    if synt and synt.inject_solution:
        problem.base_source = problem.unpack(synt.get_x())

    check_problem(problem)

    rundir = config.rundir_template % dict(
        problem_name=problem.name)

    if op.exists(rundir):
        if force:
            shutil.rmtree(rundir)
        else:
            logger.warn('skipping problem %s: rundir already exists: %s' %
                        (problem.name, rundir))
            return

    util.ensuredir(rundir)

    logger.info(
        'start %i / %i' % (ievent+1, nevents))

    analyse(
        problem,
        niter=config.analyser_config.niter,
        show_progress=nparallel == 1)

    basepath = config.get_basepath()
    config.change_basepath(rundir)
    guts.dump(config, filename=op.join(rundir, 'config.yaml'))
    config.change_basepath(basepath)

    problem.dump_problem_info(rundir)

    xs_inject = None
    synt = ds.synthetic_test
    if synt and synt.inject_solution:
        xs_inject = synt.get_x()[num.newaxis, :]

    solve(problem,
          rundir=rundir,
          status=status,
          xs_inject=xs_inject,
          **config.solver_config.get_solver_kwargs())

    harvest(rundir, problem)

    tstop = time.time()
    logger.info(
        'stop %i / %i (%g min)' % (ievent, nevents, (tstop - tstart)/60.))


class ParameterStats(Object):
    name = String.T()
    mean = Float.T()
    std = Float.T()
    best = Float.T()
    minimum = Float.T()
    percentile5 = Float.T()
    percentile16 = Float.T()
    median = Float.T()
    percentile84 = Float.T()
    percentile95 = Float.T()
    maximum = Float.T()

    def __init__(self, *args, **kwargs):
        kwargs.update(zip(self.T.propnames, args))
        Object.__init__(self, **kwargs)


class ResultStats(Object):
    problem = Problem.T()
    parameter_stats_list = List.T(ParameterStats.T())


def make_stats(problem, xs, misfits, pnames=None):
    gms = problem.global_misfits(misfits)
    ibest = num.argmin(gms)
    rs = ResultStats(problem=problem)
    if pnames is None:
        pnames = problem.parameter_names

    for pname in pnames:
        iparam = problem.name_to_index(pname)
        vs = problem.extract(xs, iparam)
        mi, p5, p16, median, p84, p95, ma = map(float, num.percentile(
            vs, [0., 5., 16., 50., 84., 95., 100.]))

        mean = float(num.mean(vs))
        std = float(num.std(vs))
        best = float(vs[ibest])
        s = ParameterStats(
            pname, mean, std, best, mi, p5, p16, median, p84, p95, ma)

        rs.parameter_stats_list.append(s)

    return rs


def format_stats(rs, fmt):
    pname_to_pindex = dict(
        (p.name, i) for (i, p) in enumerate(rs.parameter_stats_list))

    values = []
    headers = []
    for x in fmt:
        pname, qname = x.split('.')
        pindex = pname_to_pindex[pname]
        values.append(getattr(rs.parameter_stats_list[pindex], qname))
        headers.append(x)

    return ' '.join('%16.7g' % v for v in values)


def export(what, rundirs, type=None, pnames=None, filename=None):
    if pnames is not None:
        pnames_clean = [pname.split('.')[0] for pname in pnames]
        shortform = all(len(pname.split('.')) == 2 for pname in pnames)
    else:
        pnames_clean = None
        shortform = False

    if what == 'stats' and type is not None:
        raise GrondError('invalid argument combination: what=%s, type=%s' % (
            repr(what), repr(type)))

    if what != 'stats' and shortform:
        raise GrondError('invalid argument combination: what=%s, pnames=%s' % (
            repr(what), repr(pnames)))

    if what != 'stats' and type != 'vector' and pnames is not None:
        raise GrondError(
            'invalid argument combination: what=%s, type=%s, pnames=%s' % (
                repr(what), repr(type), repr(pnames)))

    if filename is None:
        out = sys.stdout
    else:
        out = open(filename, 'w')

    if type is None:
        type = 'event'

    if shortform:
        print >>out, '#', ' '.join('%16s' % x for x in pnames)

    def dump(x, indices):
        if type == 'vector':
            print >>out, ' ', ' '.join('%16.7g' % v for v in x[indices])

        elif type == 'source':
            source = problem.unpack(x)
            guts.dump(source, stream=out)

        elif type == 'event':
            ev = problem.unpack(x).pyrocko_event()
            model.dump_events([ev], stream=out)

        else:
            raise GrondError('invalid argument: type=%s' % repr(type))

    header = None
    for rundir in rundirs:
        problem, xs, misfits = load_problem_info_and_data(
            rundir, subset='harvest')

        if type == 'vector':
            pnames_take = pnames_clean or \
                problem.parameter_names[:problem.nparameters]

            indices = num.array(
                [problem.name_to_index(pname) for pname in pnames_take])

            new_header = '# ' + ' '.join('%16s' % x for x in pnames_take)
            if type == 'vector' and header != new_header:
                print >>out, new_header

            header = new_header
        else:
            indices = None

        if what == 'best':
            dump(get_best_x(problem, xs, misfits), indices)

        elif what == 'mean':
            dump(get_mean_x(xs), indices)

        elif what == 'ensemble':
            gms = problem.global_misfits(misfits)
            isort = num.argsort(gms)
            for i in isort:
                dump(xs[i], indices)

        elif what == 'stats':
            rs = make_stats(problem, xs, misfits, pnames_clean)
            if shortform:
                print >>out, ' ', format_stats(rs, pnames)
            else:
                print >>out, rs

        else:
            raise GrondError('invalid argument: what=%s' % repr(what))

    if out is not sys.stdout:
        out.close()


__all__ = '''
    GrondError
    Parameter
    ADict
    Path
    Problem
    ProblemConfig
    MisfitTarget
    MisfitResult
    Forbidden
    InnerMisfitConfig
    DatasetConfig
    TargetConfig
    SamplerDistributionChoice
    SolverConfig
    EngineConfig
    Config
    HasPaths
    TargetAnalysisResult
    load_problem_info_and_data
    read_config
    forward
    harvest
    go
    check
    export
'''.split()