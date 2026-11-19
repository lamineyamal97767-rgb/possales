-- Migration: 003_printer_ink_compatibility.sql
-- Description: Add application-owned table for verified Printer <-> Ink dependencies.
-- Rollback: See DOWN section below.

-- ============================================================================
-- UP MIGRATION
-- ============================================================================

CREATE TABLE IF NOT EXISTS printer_ink_compatibility (
    id INT AUTO_INCREMENT PRIMARY KEY,
    printer_id VARCHAR(10) NOT NULL,
    ink_id VARCHAR(10) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    source VARCHAR(100) NOT NULL DEFAULT 'Admin confirmed',
    notes TEXT NULL,
    created_by VARCHAR(50) DEFAULT 'admin',
    updated_by VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_printer_ink (printer_id, ink_id),
    KEY idx_pic_printer_status (printer_id, status),
    KEY idx_pic_ink_status (ink_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================================
-- DOWN MIGRATION (ROLLBACK)
-- ============================================================================
-- DROP TABLE IF EXISTS printer_ink_compatibility;
