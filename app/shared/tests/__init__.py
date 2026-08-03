"""Unit tests for the shared agent library.

These live *inside* the ``shared`` package (and use relative imports) so they
ride the ``shared`` symlink into every scaffolded project as ``app/shared/tests``
— exercising the same code both here (as ``shared.tests``) and in generated
projects (as ``app.shared.tests``) with no import rewriting.
"""
