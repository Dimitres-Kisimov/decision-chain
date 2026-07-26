"""decision-chain: one real dataset through the whole distributor decision chain.

Phase 1 implements stages 0 (ingest) and 1 (forecast) plus the reconciliation
harness (stage 6). Stages 2-5 are declared as contracts in ``chain.contracts``
and arrive in later phases.
"""

__version__ = "0.1.0"
