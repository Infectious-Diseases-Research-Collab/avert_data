-- Uganda MRC codes and facility names, for MySQL.
-- Used by daily_site_counts_mysql.sql. Safe to re-run: drops and recreates
-- the table. mrc is text so leading zeros are kept ('027', not 27).

DROP TABLE IF EXISTS ug_mrcs;

CREATE TABLE ug_mrcs (
    mrc   CHAR(3)      NOT NULL PRIMARY KEY,
    name  VARCHAR(100) NOT NULL
);

INSERT INTO ug_mrcs (mrc, name) VALUES
    ('024', 'Amuru HCIII'),
    ('027', 'Lalogi HCIV'),
    ('031', 'Namokora HCIV'),
    ('032', 'Kitgum Matidi HCIII'),
    ('034', 'Lira-Kato HCIII'),
    ('036', 'Otwal HCIII'),
    ('049', 'Kamaca HCIII'),
    ('052', 'Bukedea HCIV'),
    ('053', 'Kidera HCIV'),
    ('054', 'Bugaya HCIII'),
    ('055', 'Bumanya HCIV'),
    ('056', 'Nawaikoke HCIII'),
    ('058', 'Ikumbya HCIII'),
    ('062', 'Kigandalo HCIV'),
    ('063', 'Lumino HCIII'),
    ('069', 'Orum HCIV'),
    ('071', 'Nadunget HCIII'),
    ('079', 'Rwenyawawa HCIII');
