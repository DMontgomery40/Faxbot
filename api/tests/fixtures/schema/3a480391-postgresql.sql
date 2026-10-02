-- Frozen fixture from 3a480391:api/app/db.py; no current application model import.

CREATE TABLE api_keys (
	id VARCHAR(40) NOT NULL, 
	key_id VARCHAR(32) NOT NULL, 
	key_hash VARCHAR(200) NOT NULL, 
	name VARCHAR(100), 
	owner VARCHAR(100), 
	scopes VARCHAR(200), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	last_used_at TIMESTAMP WITHOUT TIME ZONE, 
	expires_at TIMESTAMP WITHOUT TIME ZONE, 
	revoked_at TIMESTAMP WITHOUT TIME ZONE, 
	note TEXT, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_api_keys_id ON api_keys (id);

CREATE UNIQUE INDEX ix_api_keys_key_id ON api_keys (key_id);

CREATE TABLE fax_jobs (
	id VARCHAR(40) NOT NULL, 
	to_number VARCHAR(64) NOT NULL, 
	file_name VARCHAR(255) NOT NULL, 
	tiff_path VARCHAR(512) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	error TEXT, 
	pages INTEGER, 
	backend VARCHAR(20) NOT NULL, 
	provider_sid VARCHAR(100), 
	pdf_url VARCHAR(512), 
	pdf_token VARCHAR(128), 
	pdf_token_expires_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_fax_jobs_id ON fax_jobs (id);

CREATE INDEX ix_fax_jobs_status ON fax_jobs (status);

CREATE INDEX ix_fax_jobs_to_number ON fax_jobs (to_number);

CREATE TABLE inbound_events (
	id VARCHAR(40) NOT NULL, 
	provider_sid VARCHAR(100) NOT NULL, 
	event_type VARCHAR(50) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uix_inbound_events_sid_type UNIQUE (provider_sid, event_type)
);

CREATE INDEX ix_inbound_events_id ON inbound_events (id);

CREATE TABLE inbound_faxes (
	id VARCHAR(40) NOT NULL, 
	from_number VARCHAR(64), 
	to_number VARCHAR(64), 
	status VARCHAR(32) NOT NULL, 
	backend VARCHAR(20) NOT NULL, 
	provider_sid VARCHAR(100), 
	pages INTEGER, 
	size_bytes INTEGER, 
	sha256 VARCHAR(64), 
	pdf_path VARCHAR(512), 
	tiff_path VARCHAR(512), 
	mailbox_label VARCHAR(100), 
	retention_until TIMESTAMP WITHOUT TIME ZONE, 
	pdf_token VARCHAR(128), 
	pdf_token_expires_at TIMESTAMP WITHOUT TIME ZONE, 
	error TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_inbound_faxes_from_number ON inbound_faxes (from_number);

CREATE INDEX ix_inbound_faxes_id ON inbound_faxes (id);

CREATE INDEX ix_inbound_faxes_status ON inbound_faxes (status);

CREATE INDEX ix_inbound_faxes_to_number ON inbound_faxes (to_number);

CREATE TABLE inbound_rules (
	id VARCHAR(40) NOT NULL, 
	to_number VARCHAR(64) NOT NULL, 
	mailbox_label VARCHAR(100) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_inbound_rules_id ON inbound_rules (id);

CREATE INDEX ix_inbound_rules_to_number ON inbound_rules (to_number);

CREATE TABLE mailboxes (
	id VARCHAR(40) NOT NULL, 
	label VARCHAR(100) NOT NULL, 
	allowed_scopes VARCHAR(200), 
	note TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (label)
);

CREATE INDEX ix_mailboxes_id ON mailboxes (id);
