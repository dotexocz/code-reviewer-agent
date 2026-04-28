"""Multi-agent code reviewer postavený na Claude Agent SDK.

Implementuje vzor *Supervisor + Parallel*:

  ┌─────────────┐
  │ Supervisor  │  ← spojí výstupy do finálního reportu
  └──────┬──────┘
         │
   ┌─────┼─────┐
   ▼     ▼     ▼
 Sec   Perf  Style       ← běží paralelně na rychlejším modelu
"""

__version__ = "0.1.0"
