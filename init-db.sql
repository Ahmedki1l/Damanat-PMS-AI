-- init-db.sql
-- Creates the damanat login, database, and user
-- This runs once after SQL Server is healthy

-- Create the login (SQL Server level)
IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = 'damanat')
BEGIN
    CREATE LOGIN damanat WITH PASSWORD = 'damanat', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF;
    PRINT 'Login damanat created.';
END
ELSE
BEGIN
    PRINT 'Login damanat already exists.';
END
GO

-- Create the database
IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'damanat_pms')
BEGIN
    CREATE DATABASE damanat_pms;
    PRINT 'Database damanat_pms created.';
END
ELSE
BEGIN
    PRINT 'Database damanat_pms already exists.';
END
GO

-- Switch to the new database and create user
USE damanat_pms;
GO

IF NOT EXISTS (SELECT name FROM sys.database_principals WHERE name = 'damanat')
BEGIN
    CREATE USER damanat FOR LOGIN damanat;
    PRINT 'User damanat created in damanat_pms.';
END
ELSE
BEGIN
    PRINT 'User damanat already exists in damanat_pms.';
END
GO

-- Grant full ownership permissions
IF NOT EXISTS (
    SELECT 1
    FROM sys.database_role_members drm
    INNER JOIN sys.database_principals role_principal
        ON drm.role_principal_id = role_principal.principal_id
    INNER JOIN sys.database_principals member_principal
        ON drm.member_principal_id = member_principal.principal_id
    WHERE role_principal.name = 'db_owner'
      AND member_principal.name = 'damanat'
)
BEGIN
    ALTER ROLE db_owner ADD MEMBER damanat;
    PRINT 'User damanat added to db_owner role.';
END
ELSE
BEGIN
    PRINT 'User damanat already has db_owner role.';
END
GO
