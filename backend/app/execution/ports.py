"""Execution uses the framework's public graph/checkpointer interfaces directly.

Product ports live beside their owning integration (`device.contracts`,
`tools.contracts`, and `llm.contracts`); no parallel execution DTO hierarchy is
defined here.
"""
