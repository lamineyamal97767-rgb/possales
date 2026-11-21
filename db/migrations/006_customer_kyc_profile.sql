-- Report 06: Customer Business KYC
-- Additive migration: customer_kyc_profile table

CREATE TABLE IF NOT EXISTS customer_kyc_profile (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_id VARCHAR(10) NOT NULL UNIQUE,
    business_name VARCHAR(200) NULL,
    business_type VARCHAR(100) NULL,
    contact_person VARCHAR(100) NULL,
    notes TEXT NULL,
    updated_by_user_id INT NULL,
    updated_by_username VARCHAR(50) DEFAULT 'admin',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_ckp_customer (customer_id),
    KEY idx_ckp_type (business_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
