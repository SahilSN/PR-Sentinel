"""Tiny user lookup service, used to demo PR Sentinel on a live pull request."""

USERS = {
    "alice": {"name": "Alice", "email": "alice@example.com"},
    "bob": {"name": "Bob", "email": "bob@example.com"},
}


def find_user(username):
    """Return the user record, or None if there is no such user."""
    return USERS.get(username.lower())


def greeting(username):
    user = find_user(username)
    return f"Hello, {user['name']}!"


def contact_line(username):
    user = find_user(username)
    if user is None:
        return "No contact on file."
    return f"{user['name']} <{user['email']}>"
