"""pov - POV execution engine adapter package.

Also hosts the ``slicers`` sub-package (the execution algorithms that decide
HOW to slice orders over time) -- order slicing is the POV stage's job.
"""
from .pov_engine import PovEngine
from . import slicers

__all__ = ["PovEngine", "slicers"]