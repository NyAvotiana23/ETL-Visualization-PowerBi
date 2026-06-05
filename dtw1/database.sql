CREATE DATABASE datawarehouse_tp1;


psql -U postgres -d datawarehouse_tp1 -f 05_load_postgres.sql

psql -U postgres -d datawarehouse_tp1 -f data_detail.sql > detail.txt