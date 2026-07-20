"""Composable Rich building blocks for the Mission Control UI.

Each widget is a stateless renderable factory: takes view data, returns
a Rich renderable. Widgets never call the event bus or mutate state.
"""
