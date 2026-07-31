"""Integration tests against a containerised IMAP server (contracts §6.1,
§6.2; specification §16 point 7). Everything under this package is marked
`@pytest.mark.integration` (via each module's `pytestmark`) and skips
cleanly when Docker is unavailable -- see `conftest.py`.
"""
