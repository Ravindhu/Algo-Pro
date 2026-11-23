"""
optim/hrp.py - Hierarchical Risk Parity (HRP) and Nested Clustered Optimisation.

Lopez de Prado's HRP fixes the two failure modes of mean-variance:
  1. it never *inverts* a noisy covariance matrix (so it does not amplify the
     estimation error in the small eigenvalues), and
  2. it allocates across *clusters* first, so correlated sleeves cannot sneak
     into the book as if they were independent bets.

Steps (exactly Lopez de Prado, "Building Diversified Portfolios that Outperform
Out-of-Sample", 2016):
  1. **Tree clustering** — convert the correlation matrix to a distance
     ``d_ij = sqrt(0.5 (1 - rho_ij))`` and build a single-linkage dendrogram.
  2. **Quasi-diagonalisation** — reorder assets so similar ones sit together.
  3. **Recursive bisection** — split the ordered list in half; give each half a
     weight inversely proportional to its *cluster* variance (``w_cluster =
     (1/V) / (1/V_L + 1/V_R)``), recursing to leaves.

NCO then runs the base optimiser *within* each cluster and HRP *across* clusters,
which is the practical best-of-both.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import BaseOptimiser, ConstraintSpec, normalise_weights
from ..risk.covariance import corr_from_cov
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["hrp_weights", "cluster_order", "quasi_diag", "HRP", "NCO"]


def _corr_dist(corr: Sequence[Sequence[float]]) -> List[List[float]]:
    """Correlation -> distance matrix ``d = sqrt(0.5 (1 - rho))`` (clipped)."""
    n = len(corr)
    d = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            rho = max(min(float(corr[i][j]), 1.0), -1.0)
            d[i][j] = math.sqrt(max(0.5 * (1.0 - rho), 0.0))
    return d


def _cluster_var(cov: Sequence[Sequence[float]], idx: Sequence[int]
                 ) -> float:
    """Inverse-variance-weighted variance of a cluster (Lopez de Prado)."""
    ivp = [1.0 / cov[i][i] if cov[i][i] > 0 else 0.0 for i in idx]
    s = sum(ivp)
    if s <= 0:
        # Fall back to equal weights.
        k = len(idx)
        ivp = [1.0 / k] * k
    else:
        ivp = [x / s for x in ivp]
    var = 0.0
    for a, i in enumerate(idx):
        for b, j in enumerate(idx):
            var += ivp[a] * ivp[b] * float(cov[i][j])
    return max(var, 1e-18)


def quasi_diag(link: List[Tuple[int, int, float]], n: int) -> List[int]:
    """Recover the quasi-diagonal leaf order from a linkage matrix.

    Implements Lopez de Prado's algorithm exactly: start from the final merge
    and recursively expand each internal node into its two children, appending
    leaves as they are reached. Leaves are ids ``0..n-1``; the ``k``-th merge is
    internal node ``n + k``.
    """
    if n <= 1:
        return list(range(n))
    link = list(link)
    if not link:
        return list(range(n))
    order: List[int] = []
    # Stack of node ids to expand; begin with the two children of the root merge.
    # Push right, then left, so the LEFT child of the root is expanded first (same as inner nodes).
    stack: List[int] = [int(link[-1][1]), int(link[-1][0])]
    while stack:
        node = stack.pop()
        if node < n:
            order.append(node)
        else:
            left, right = int(link[node - n][0]), int(link[node - n][1])
            # Push right then left so left is expanded first (in-order DFS).
            stack.append(right)
            stack.append(left)
    if len(order) != n:
        return list(range(n))
    return order


def _single_linkage(dist: Sequence[Sequence[float]], n: int
                    ) -> List[Tuple[int, int, float]]:
    """Single-linkage agglomerative clustering (pure Python).

    Returns a linkage list of ``(cluster_a, cluster_b, distance)`` where leaves
    are ``0..n-1`` and each new merge appends cluster id ``n + k``. This mirrors
    ``scipy.cluster.hierarchy.linkage(..., method='single')`` output shape so the
    same quasi-diagonalisation applies.
    """
    # Active clusters: id -> member leaf indices.
    clusters: Dict[int, List[int]] = {i: [i] for i in range(n)}
    # Working distance between active clusters = min pairwise distance.
    dist_between: Dict[Tuple[int, int], float] = {}
    for a in range(n):
        for b in range(a + 1, n):
            dist_between[(a, b)] = dist[a][b]

    link: List[Tuple[int, int, float]] = []
    next_id = n
    active = list(range(n))
    while len(active) > 1:
        # Find the closest pair.
        best = None
        best_d = float("inf")
        for x in range(len(active)):
            for y in range(x + 1, len(active)):
                a, b = active[x], active[y]
                key = (a, b) if a < b else (b, a)
                d = dist_between.get(key, float("inf"))
                if d < best_d:
                    best_d = d
                    best = (a, b)
        if best is None:
            break
        a, b = best
        link.append((a, b, best_d))
        # Merge b into a; new cluster id.
        clusters[next_id] = clusters[a] + clusters[b]
        # Update distances: single linkage = min over cross pairs.
        for other in active:
            if other in (a, b):
                continue
            d_ao = dist_between.get((min(a, other), max(a, other)), float("inf"))
            d_bo = dist_between.get((min(b, other), max(b, other)), float("inf"))
            dist_between[(min(next_id, other), max(next_id, other))] = min(d_ao, d_bo)
        active = [c for c in active if c not in (a, b)]
        active.append(next_id)
        next_id += 1
    return link


def cluster_order(corr: Sequence[Sequence[float]]) -> List[int]:
    """Return the HRP quasi-diagonal asset order for a correlation matrix."""
    n = len(corr)
    if n <= 1:
        return list(range(n))
    dist = _corr_dist(corr)
    link = _single_linkage(dist, n)
    return quasi_diag(link, n)


def hrp_weights(cov: Sequence[Sequence[float]], *,
                corr: Optional[Sequence[Sequence[float]]] = None
                ) -> Optional[List[float]]:
    """Hierarchical Risk Parity weights (Lopez de Prado).

    Returns a list aligned to the original asset order (not the clustered order),
    long-only, summing to 1. None on a degenerate covariance.
    """
    n = len(cov or [])
    if n == 0:
        return None
    corr = corr or corr_from_cov(cov)
    if corr is None:
        return None
    order = cluster_order(corr)
    w = _recursive_bisection(cov, order)
    if w is None:
        return None
    # w is indexed by position within `order`; scatter back to original indices.
    out = [0.0] * n
    for pos, asset_idx in enumerate(order):
        out[asset_idx] = w[pos]
    total = sum(out)
    if total <= 0:
        return None
    return [x / total for x in out]


def _recursive_bisection(cov: Sequence[Sequence[float]],
                         order: Sequence[int]) -> Optional[List[float]]:
    """Recursive bisection over the quasi-diagonal order.

    Works on *positions* into ``order`` (0..m-1) and carries a weight list sized
    to the order, so the slice arithmetic lines up with Lopez de Prado's
    ``getRecBipart``: split the ordered list in half, scale each half by
    ``1 - V_left/(V_left+V_right)`` and ``V_left/(V_left+V_right)`` respectively
    (i.e. the lower-variance half gets the larger weight), then recurse.
    """
    m = len(order)
    w = [1.0] * m
    items = list(order)

    def _recurse(idx: List[int]) -> None:
        if len(idx) <= 1:
            return
        mid = len(idx) // 2
        left_pos, right_pos = idx[:mid], idx[mid:]
        # Map positions -> original asset ids for the variance computation.
        left_assets = [items[p] for p in left_pos]
        right_assets = [items[p] for p in right_pos]
        var_l = _cluster_var(cov, left_assets)
        var_r = _cluster_var(cov, right_assets)
        alpha = 1.0 - var_l / (var_l + var_r) if (var_l + var_r) > 0 else 0.5
        for p in left_pos:
            w[p] *= alpha
        for p in right_pos:
            w[p] *= (1.0 - alpha)
        _recurse(left_pos)
        _recurse(right_pos)

    _recurse(list(range(m)))
    return w


class HRP(BaseOptimiser):
    """Hierarchical Risk Parity optimiser."""

    name = "hrp"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if cov and len(cov) >= n:
            c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
            return hrp_weights(c)
        # No covariance -> inverse-vol fallback.
        vols = problem.get("vols") or {}
        from .risk_parity import inverse_vol_weights
        self.log("no covariance; inverse-vol fallback for HRP")
        return inverse_vol_weights([safe_float(vols.get(u)) or 0.2
                                    for u in universe])


class NCO(BaseOptimiser):
    """Nested Clustered Optimisation: HRP across clusters, ERC within.

    A pragmatic best-of-both: use HRP's cluster structure, but inside each
    cluster allocate with true ERC (correlation-aware) rather than the HRP
    inverse-variance split. In practice this is the "does everything well"
    default when you trust the correlation matrix but not enough to invert it.
    """

    name = "nco"

    def _optimise(self, problem: Dict[str, Any], spec: ConstraintSpec
                  ) -> Optional[List[float]]:
        universe = list(problem.get("universe") or [])
        n = len(universe)
        cov = problem.get("cov")
        if not cov or len(cov) < n:
            from .risk_parity import inverse_vol_weights
            vols = problem.get("vols") or {}
            return inverse_vol_weights([safe_float(vols.get(u)) or 0.2
                                        for u in universe])
        c = [[float(cov[i][j]) for j in range(n)] for i in range(n)]
        corr = corr_from_cov(c) or []
        order = cluster_order(corr) if corr else list(range(n))
        # Split the order in half; ERC within each half; HRP across the halves.
        from .risk_parity import erc_weights
        mid = len(order) // 2
        left, right = order[:mid], order[mid:] or order[:mid]
        w = [0.0] * n
        for half in (left, right):
            sub = [[c[i][j] for j in half] for i in half]
            w_sub = erc_weights(sub) or normalise_weights([1.0] * len(half))
            for pos, i in enumerate(half):
                w[i] = w_sub[pos]
        total = sum(w)
        return [x / total for x in w] if total > 0 else None