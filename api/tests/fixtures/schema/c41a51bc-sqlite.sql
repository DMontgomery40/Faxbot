-- Frozen fixture from c41a51bc:api/app/db.py; no current application model import.

CREATE TABLE fax_jobs (
	id VARCHAR(40) NOT NULL, 
	to_number VARCHAR(64) NOT NULL, 
	file_name VARCHAR(255) NOT NULL, 
	tiff_path VARCHAR(512) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	error TEXT, 
	pages INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_fax_jobs_id ON fax_jobs (id);

CREATE INDEX ix_fax_jobs_status ON fax_jobs (status);

CREATE INDEX ix_fax_jobs_to_number ON fax_jobs (to_number);
