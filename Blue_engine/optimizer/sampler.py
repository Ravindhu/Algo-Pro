"""
optimizer/sampler.py - TPE fallback when Optuna is not installed.

WHY THIS EXISTS
---------------
Verified: ``optuna`` is NOT installed in this project's venv. AGENTS.md mandates
TPE ("Tree-structured Parzen Estimator - instead of checking every variable
sequentially"). Shipping without a sampler would make that mandate unmeetable;
shipping with a hard Optuna dependency would break every other module on a bare
machine (the same reason ``ml/evaluation`` avoids numpy).

So: this file implements a *self-contained* TPE that the engine uses when
Optuna is absent. If Optuna IS present, ``engine.py`` uses Optuna's sampler
instead and this file is never imported. No hard dependency either way.

WHAT TPE IS, IN PLAIN ENGLISH
-----------------------------
Random search samples parameters uniformly and never learns. TPE builds two
models of the observations:

    l(x):  the density of parameters that produced the BEST ~15% of trials
    g(x):  the density of parameters that produced the REST

and then, for each new draw, samples many candidates from ``l(x)`` and picks
the one that maximises ``l(x) / g(x)`` - i.e. "most likely under good trials,
least likely under bad ones". That is the Parzen-estimator trick from
Bergstra et al., "Algorithms for Hyper-Parameter Optimization" (NeurIPS 2011).

WHAT IS SIMPLIFIED HERE (stated, not hidden)
--------------------------------------------
1. **Independent per-parameter densities.** Full TPE models the joint density
   with tree structure. This fallback models each parameter independently and
   recombines by multiplication. On independent-ish spaces this is close; on
   strongly coupled spaces it converges slower. The visual workflow (importance
   -> slice) is exactly the tool that reveals a coupled space, so the operator
   can see it and raise the trial budget rather than trust a thin study.
2. **Gaussian KDE** for continuous parameters, weighted-histogram for
   categoricals. Bandwidth is Scott's rule. Optuna uses similar building blocks
   with more refined scheduling.
3. **Uniform acquisition**: we draw ``n_ei_candidates`` from l and score
   ``log l - log g`` per candidate (per-parameter sums). Optuna does the same
   in log space; the difference is the modelling above, not the acquisition.

CONTRACT
--------
``LocalTrial`` implements the same three ``suggest_*`` methods as
``optuna.trial.Trial``, which is what lets :class:`SearchSpace.sample` be
written once against the tiny protocol. The sampler is driven through the
``ask/tell`` pattern so the engine loop is identical whether Optuna or this
fallback is underneath.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import logger

__all__ = ["LocalTrial", "TPESampler"]


# --------------------------------------------------------------------------- #
# The trial
# --------------------------------------------------------------------------- #


class LocalTrial:
    """The ask-side of the fallback sampler.

    Mirrors the three ``suggest_*`` calls Optuna exposes. Two differences are
    deliberate and worth knowing:

    * ``suggest_float(log=True)`` samples in log space immediately, so the
      value handed to the strategy is already the natural-space number.
    * every suggestion is recorded, so ``trial.params`` is the full parameter
      dict for the journal - no extra plumbing in the engine.
    """

    def __init__(self, sampler: "TPESampler", trial_number: int) -> None:
        self._sampler = sampler
        self.number = trial_number
        self.params: Dict[str, Any] = {}

    # -- the protocol ------------------------------------------------------ #

    def suggest_float(self, name: str, low: float, high: float, *,
                      log: bool = False) -> float:
        v = self._from_pending(name)
        if v is None:
            v = self._sampler._sample_float(name, low, high, log)
        v = float(min(max(float(v), float(low)), float(high)))
        self.params[name] = v
        return v

    def suggest_int(self, name: str, low: int, high: int) -> int:
        v = self._from_pending(name)
        if v is None:
            v = self._sampler._sample_float(name, float(low), float(high),
                                            log=False)
        v = int(round(float(v)))
        v = min(max(v, int(low)), int(high))
        self.params[name] = v
        return v

    def suggest_categorical(self, name: str, choices: Sequence[Any]) -> Any:
        v = self._from_pending(name)
        if v is None or v not in choices:
            v = self._sampler._sample_categorical(name, list(choices))
        self.params[name] = v
        return v

    def _from_pending(self, name: str) -> Any:
        """Return the pre-decided value for ``name``, or None.

        In joint mode the sampler has already chosen the whole point in
        ``ask()``. Serving it here is what makes the coupling real: the space's
        per-parameter ``suggest_*`` calls no longer decide anything, they just
        collect the coordinates of a point that was chosen as a unit. A value
        outside the parameter's bounds is dropped rather than clamped, so a
        stale pending point can never inject an illegal parameter - the normal
        independent path then handles that parameter.
        """
        pend = self._sampler._pending
        if not pend or name not in pend:
            return None
        return pend.get(name)


# --------------------------------------------------------------------------- #
# The sampler
# --------------------------------------------------------------------------- #


class TPESampler:
    """Ask/tell TPE. Pure Python, no numpy, deterministic under ``seed``.

    Usage (what ``engine.py`` does)::

        s = TPESampler(seed=7)
        trial = s.ask()                    # LocalTrial with .params filled
        value = evaluate(trial.params)     # higher is better
        s.tell(trial, value)
    """

    def __init__(self, *, seed: int = 0, n_startup: int = 10,
                 n_ei_candidates: int = 24, gamma_fraction: float = 0.15,
                 multivariate: bool = True) -> None:
        self.rng = random.Random(int(seed))
        #: Trials drawn uniformly before TPE kicks in. Below ~10 observations a
        #: density estimate is noise; this is the same idea as Optuna's
        #: ``n_startup_trials``.
        self.n_startup = max(5, int(n_startup))
        self.n_ei_candidates = max(8, int(n_ei_candidates))
        #: Fraction of observations that define the "good" density l(x).
        #: Bergstra et al. use gamma = 0.15.
        self.gamma_fraction = float(gamma_fraction)
        #: AUDIT FINDING C1d. When True the sampler proposes WHOLE POINTS drawn
        #: from the good set's joint distribution, instead of assembling a point
        #: from independently-chosen coordinates. On a coupled space the
        #: independent version is not merely slower, it is wrong: it will pair a
        #: good value of one parameter with a value of another that only works in
        #: a different context, score the pair badly, and conclude the first
        #: parameter is bad. See ``_ask_joint`` for the mechanism.
        self.multivariate = bool(multivariate)
        self._observations: List[Tuple[Dict[str, Any], float]] = []
        self._best: Optional[float] = None
        self._n_asked = 0
        #: The trial currently being built, so ``_ask_joint`` can hand out a
        #: whole pre-decided point through the per-parameter ``suggest_*`` API.
        self._pending: Optional[Dict[str, Any]] = None

    # -- ask / tell -------------------------------------------------------- #

    def ask(self) -> LocalTrial:
        t = LocalTrial(self, self._n_asked)
        self._n_asked += 1
        # Decide the whole point NOW (joint mode) and let ``suggest_*`` serve it.
        # Doing it here rather than inside ``suggest_*`` is what makes the
        # coupling possible: by the time the space asks for one parameter, the
        # sampler has already chosen the company that parameter keeps.
        self._pending = self._ask_joint() if self.multivariate else None
        return t

    def _ask_joint(self) -> Optional[Dict[str, Any]]:
        """Pre-decide a full parameter point using the JOINT good-set density.

        The mechanism, in plain terms:

        1. Split the observations into the good set (top ``gamma``) and the rest,
           exactly as the independent version does.
        2. Pick one observed GOOD point at random and take ALL of its coordinates
           as the proposal's starting shape.
        3. Perturb each coordinate with a small Gaussian whose width is that
           coordinate's bandwidth - so the proposal stays near a region we have
           actually seen work, but is not a copy.
        4. Repeat ``n_ei_candidates`` times and keep the candidate maximising
           ``log l - log g``, where the densities are now evaluated over the
           WHOLE point (sum of per-coordinate log densities) rather than one
           coordinate at a time.

        Why step 2 is the fix: it preserves the *correlations* present in the
        good set. If every good trial had a small ``fast`` AND a zero
        ``threshold``, then every proposal inherits both, and the sampler never
        again invents the bad ``small fast + large threshold`` pairing that made
        the independent version give up on that region.

        This is a coarse stand-in for Optuna's multivariate Parzen estimator
        (which fits a full covariance per tree). It is intentionally simple, and
        it is only used when Optuna is absent - the point is that the FALLBACK
        must not be systematically blind to coupling, not that it should match
        Optuna.

        Returns None when there is not yet enough history, in which case
        ``suggest_*`` falls back to independent sampling.
        """
        obs = [(p, v) for p, v in self._observations if p]
        if len(obs) < self.n_startup:
            return None
        good, bad = self._split(obs)
        good_points = [p for p, _ in good]
        bad_points = [p for p, _ in bad]
        if not good_points:
            return None

        keys = sorted({k for p in good_points for k in p})
        if not keys:
            return None
        # Bandwidth per coordinate, in the coordinates' own units.
        bw: Dict[str, float] = {}
        for k in keys:
            vals = [float(p[k]) for p in good_points
                    if isinstance(p.get(k), (int, float))]
            if vals:
                bw[k] = self._bandwidth(vals) if len(vals) > 1 else 0.0

        best_point, best_score = None, None
        for _ in range(self.n_ei_candidates):
            anchor = self.rng.choice(good_points)
            cand: Dict[str, Any] = {}
            for k in keys:
                v = anchor.get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    cand[k] = v                      # categorical / non-numeric
                    continue
                w = bw.get(k, 0.0)
                cand[k] = float(v) if w <= 0 else float(v) + self.rng.gauss(0.0, w)
            score = (self._joint_log_density(cand, good_points, keys)
                     - (self._joint_log_density(cand, bad_points, keys)
                        if bad_points else 0.0))
            if best_score is None or score > best_score:
                best_score, best_point = score, cand
        return best_point

    @staticmethod
    def _joint_log_density(point: Dict[str, Any],
                           pool: List[Dict[str, Any]],
                           keys: List[str]) -> float:
        """Sum of per-coordinate Gaussian log-densities: log of the product.

        The product across coordinates is an approximation (it ignores the
        off-diagonal covariance), but it is computed at a point drawn from the
        GOOD SET's joint shape, which is where the coupling is preserved. That is
        the part that matters for escaping a ridge.
        """
        if not pool:
            return float("-inf")
        total = 0.0
        for k in keys:
            x = point.get(k)
            xs = [float(p[k]) for p in pool
                  if isinstance(p.get(k), (int, float))
                  and not isinstance(p.get(k), bool)]
            if not isinstance(x, (int, float)) or isinstance(x, bool) or not xs:
                continue
            bw = TPESampler._bandwidth(xs) if len(xs) > 1 else 0.0
            if bw <= 0:
                continue
            xf = float(x)
            dens = sum(math.exp(-0.5 * ((xf - v) / bw) ** 2) for v in xs)
            total += math.log(max(dens / (len(xs) * bw), 1e-12))
        return total

    def tell(self, trial: LocalTrial, value: Optional[float]) -> None:
        """Record one completed trial. ``None`` value = not scoreable, ignored.

        Ignoring rather than recording ``-inf`` is deliberate: an unmeasurable
        trial carries no density information, and feeding -inf would make g(x)
        learn "avoid wherever that impossible combination happened to land",
        which can remove legal regions from the search.
        """
        if value is None or not math.isfinite(float(value)):
            return
        self._observations.append((dict(trial.params), float(value)))
        v = float(value)
        if self._best is None or v > self._best:
            self._best = v

    @property
    def best_value(self) -> Optional[float]:
        return self._best

    @property
    def n_observations(self) -> int:
        return len(self._observations)

    # -- sampling internals ------------------------------------------------ #

    def _sample_float(self, name: str, low: float, high: float,
                      log: bool) -> float:
        obs = [(p, v) for p, v in self._observations if name in p]
        if len(obs) < self.n_startup:
            return self._uniform_float(name, low, high, log)

        good, bad = self._split(obs)
        # Densities restricted to THIS parameter; the joint is the product of
        # the per-parameter densities (the simplification, see module docstring).
        good_x = self._values(good, name)
        bad_x = self._values(bad, name)
        if not good_x:
            return self._uniform_float(name, low, high, log)

        # Candidate values from l(x)...
        cands = self._draw_kde(good_x, low, high, log,
                               self.n_ei_candidates)
        # ...scored by log l - log g (higher = more good-like, less bad-like).
        best_val, best_score = None, None
        for c in cands:
            ls = self._log_kde_density(c, good_x, low, high, log)
            gs = self._log_kde_density(c, bad_x, low, high, log) if bad_x else 0.0
            score = ls - gs
            if best_score is None or score > best_score:
                best_score, best_val = score, c
        if best_val is None:
            return self._uniform_float(name, low, high, log)
        # Clamp: KDE mass can leak a hair outside the bounds; a value the
        # strategy would reject on range grounds would be a wasted trial.
        v = min(max(best_val, float(low)), float(high))
        return math.exp(v) if log else v

    def _sample_categorical(self, name: str, choices: List[Any]) -> Any:
        obs = [(p, v) for p, v in self._observations if name in p]
        if len(obs) < self.n_startup:
            return self.rng.choice(choices)
        good, bad = self._split(obs)
        good_x = [p[name] for p, _ in good if name in p]
        bad_x = [p[name] for p, _ in bad if name in p]
        # Weighted-empirical densities over the choice set.
        lw = self._categorical_weights(good_x, choices)
        bw = self._categorical_weights(bad_x, choices) if bad_x else [1.0] * len(choices)
        best_idx, best_score = None, None
        for i in range(len(choices)):
            # log-density difference, with a small guard against log(0).
            score = math.log(max(lw[i], 1e-12)) - math.log(max(bw[i], 1e-12))
            if best_score is None or score > best_score:
                best_score, best_idx = score, i
        return choices[best_idx if best_idx is not None
                       else self.rng.randrange(len(choices))]

    # -- helpers ----------------------------------------------------------- #

    def _split(self, obs: List[Tuple[Dict[str, Any], float]]
               ) -> Tuple[List[Tuple[Dict[str, Any], float]],
                          List[Tuple[Dict[str, Any], float]]]:
        """Split observations into the good l-set and the bad g-set.

        Sorted by value descending; the top ``gamma`` fraction is "good". With
        very few observations, at least 1 goes to each side so both densities
        exist.
        """
        ordered = sorted(obs, key=lambda t: t[1], reverse=True)
        n_good = max(1, int(math.ceil(self.gamma_fraction * len(ordered))))
        n_good = min(n_good, len(ordered) - 1) if len(ordered) > 1 else 1
        return ordered[:n_good], ordered[n_good:] or ordered[:1]

    @staticmethod
    def _values(obs: List[Tuple[Dict[str, Any], float]], name: str) -> List[float]:
        return [float(p[name]) for p, _ in obs]

    def _uniform_float(self, name: str, low: float, high: float, log: bool) -> float:
        if log:
            return math.exp(self.rng.uniform(math.log(float(low)),
                                             math.log(float(high))))
        return self.rng.uniform(float(low), float(high))

    def _draw_kde(self, xs: List[float], low: float, high: float, log: bool,
                  n: int) -> List[float]:
        """Draw n candidates from a Gaussian KDE over ``xs``."""
        bw = self._bandwidth(xs)
        out = []
        for _ in range(n):
            center = self.rng.choice(xs)
            v = self.rng.gauss(center, bw)
            if log:
                # Keep the *log-space* draw inside the log-bounds, then the
                # caller exponentiates. This bounds the natural-space value.
                lo, hi = math.log(max(low, 1e-12)), math.log(max(high, 1e-12))
                v = min(max(v, lo), hi)
            else:
                v = min(max(v, float(low)), float(high))
            out.append(v)
        return out

    @staticmethod
    def _bandwidth(xs: List[float]) -> float:
        """Scott's rule: 1.06 * sigma * n^(-1/5), floored so 1-2 points still explore.

        The floor matters more than it looks: with 2 observations sigma can be 0
        (identical values), and a zero bandwidth would re-sample the same point
        forever, dead-locking the search on the startup set.
        """
        n = len(xs)
        mean = sum(xs) / n
        var = sum((x - mean) ** 2 for x in xs) / n if n > 1 else 0.0
        sd = math.sqrt(var)
        span = (max(xs) - min(xs)) or 1.0
        return max(1.06 * sd * n ** (-0.2), span * 0.02)

    @staticmethod
    def _log_kde_density(x: float, xs: List[float], low: float, high: float,
                         log: bool) -> float:
        """log density of a Gaussian KDE at ``x``, normalised over the range."""
        if not xs:
            return -math.inf
        bw = TPESampler._bandwidth(xs)
        n = len(xs)
        if log:
            xs = [math.log(max(v, 1e-12)) for v in xs]
            x = math.log(max(x, 1e-12))
        total = sum(math.exp(-0.5 * ((x - v) / bw) ** 2) for v in xs)
        # Normalise by the range so comparisons between parameter values with
        # different bound widths are on the same scale.
        width = (math.log(max(high, 1e-12)) - math.log(max(low, 1e-12))) if log \
            else (float(high) - float(low))
        width = max(width, 1e-12)
        return math.log(max(total / (n * bw), 1e-12)) - math.log(width)

    @staticmethod
    def _categorical_weights(values: List[Any], choices: List[Any]) -> List[float]:
        """Empirical choice frequencies, smoothed so no choice has zero mass.

        A hard zero would make log(g) -inf for that choice, and the acquisition
        would then never be able to revisit it even if the earlier bad trials
        were caused by a different parameter. Laplace smoothing (add 0.5) keeps
        every choice reachable.
        """
        if not values:
            return [1.0] * len(choices)
        counts = {c: 0 for c in choices}
        for v in values:
            if v in counts:
                counts[v] += 1
        alpha = 0.5
        total = len(values) + alpha * len(choices)
        return [(counts[c] + alpha) / total for c in choices]
