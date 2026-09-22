"""Integration tests against a containerised IMAP server. Everything
under this package is marked
`@pytest.mark.integration` (via each module's `pytestmark`) and skips
cleanly when Docker is unavailable -- see `conftest.py`.
"""
