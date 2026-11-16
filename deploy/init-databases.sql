-- Database per service: у каждого сервиса своя БД и свой пользователь.
CREATE USER shipment WITH PASSWORD 'shipment';
CREATE DATABASE shipment_db OWNER shipment;
CREATE USER locker WITH PASSWORD 'locker';
CREATE DATABASE locker_db OWNER locker;
CREATE USER payment WITH PASSWORD 'payment';
CREATE DATABASE payment_db OWNER payment;
CREATE USER notification WITH PASSWORD 'notification';
CREATE DATABASE notification_db OWNER notification;
