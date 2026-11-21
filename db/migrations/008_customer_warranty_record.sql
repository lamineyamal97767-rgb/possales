-- Migration 008: Customer Warranty Records
-- Table for Report 08: Customer Warranty
-- Application-owned table linked to sales, product, and customer records.

CREATE TABLE IF NOT EXISTS customer_warranty_record (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id VARCHAR(10) NOT NULL,
    sales_id VARCHAR(15) NOT NULL,
    product_id VARCHAR(10) NOT NULL,
    sales_detail_seq FLOAT NULL,
    serial_number VARCHAR(100) NULL,
    warranty_start_date DATE NULL,
    warranty_end_date DATE NULL,
    warranty_source VARCHAR(100) DEFAULT 'Admin entry',
    notes TEXT NULL,
    updated_by_user_id INT NULL,
    updated_by_username VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_cwr_customer (customer_id),
    KEY idx_cwr_sales (sales_id),
    KEY idx_cwr_product (product_id),
    KEY idx_cwr_serial (serial_number),
    KEY idx_cwr_dates (warranty_start_date, warranty_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Ensure sidebar menu is active and points to /menus/customer-warranty with circle icon
UPDATE sidebar_menus 
SET url = '/menus/customer-warranty', icon = 'circle', is_disabled = 0 
WHERE id = 31;
