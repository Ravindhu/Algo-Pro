"""Regression: _jacobi_eig returns eigenvectors as ROWS (as documented).

Before this fix it returned them as columns. nearest_psd rebuilt the matrix from
the wrong vectors, so a valid PSD matrix came back with flipped off-diagonal
signs (2x2 example: 0.5 became -0.5). factor_model.eigen_decomposition read the
same wrong layout.
"""
import numpy as np

from blue_engine.portfolio.risk import covariance as cov
from blue_engine.portfolio.risk import factor_model


def test_eigenvectors_satisfy_Av_equals_lambda_v():
    A = np.array([[2.0, 0.5], [0.5, 1.0]])
    vals, vecs = cov._jacobi_eig(A.tolist())
    for k in range(2):
        v = np.array(vecs[k])
        assert np.allclose(A @ v, vals[k] * v, atol=1e-10)


def test_nearest_psd_leaves_a_psd_matrix_unchanged():
    A = [[2.0, 0.5], [0.5, 1.0]]
    out = np.array(cov.nearest_psd(A))
    assert np.allclose(out, np.array(A), atol=1e-12)


def test_nearest_psd_is_identity_on_random_sample_covariance():
    rng = np.random.default_rng(3)
    S = np.cov(rng.normal(size=(200, 6)), rowvar=False)
    out = np.array(cov.nearest_psd(S.tolist()))
    assert np.allclose(out, S, atol=1e-10)


def test_factor_model_vectors_are_eigenvectors():
    rng = np.random.default_rng(4)
    S = np.cov(rng.normal(size=(150, 4)), rowvar=False)
    res = factor_model.eigen_decomposition(S.tolist())
    for val, vec in zip(res["values"], res["vectors"]):
        v = np.array(vec)
        assert np.allclose(S @ v, val * v, atol=1e-6)
