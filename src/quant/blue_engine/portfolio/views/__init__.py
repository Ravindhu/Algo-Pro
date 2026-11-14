"""
views - alpha views for Black-Litterman (pure; ML / NLP / LLM).

A *view* is a forward-looking belief that nudges the equilibrium prior: "I think
BANKNIFTY beats NIFTY by 1% over the next month, and I am 60% confident". Views
are produced here and consumed by ``optim.black_litterman``.

Sources
-------
    ml_views    supervised model (GBM or a pure-Python linear model) on history
    nlp_views   lexical / FinBERT-style sentiment from headlines
    llm_note    GPT/Ollama structured view extraction + a written PM note
    base        the canonical view schema + validation

Every producer is *optional*: no sklearn/no key -> a documented fallback and a
warning, never an exception. Views are always returned in the same schema so the
optimiser and the dashboard never need to know where they came from.
"""
from . import base, llm_note, ml_views, nlp_views

__all__ = ["base", "ml_views", "nlp_views", "llm_note"]