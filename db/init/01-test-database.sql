-- Runs once, on the db volume's first start. Tests use this database so they
-- never touch the app's data.
CREATE DATABASE sakshy_test OWNER sakshy;
