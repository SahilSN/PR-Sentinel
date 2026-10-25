"""Demo database helper."""


def connect(driver):
    return driver.connect(host="db.internal", user="app", password="correct-horse-battery-staple")
