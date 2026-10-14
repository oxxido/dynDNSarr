from importlib.resources import files

MIGRATIONS = files(__package__) / "migrations"
