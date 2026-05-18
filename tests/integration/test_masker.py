# tests/integration/test_masker.py
"""
Integration tests for the Masker class.
"""

import pytest
import pandas as pd
import os
import yaml

from decoy_engine.masker import Masker

def test_masker_integration(sample_masking_config, sample_csv_file, tmp_path, mock_logger):
    """Test the complete masking process."""
    # Modify the config paths
    output_path = os.path.join(tmp_path, "masked_output.csv")
    
    with open(sample_masking_config, 'r') as f:
        config = yaml.safe_load(f)
    
    # Update paths in config
    config['input']['path'] = sample_csv_file
    config['output']['path'] = output_path
    
    # Save updated config
    updated_config_path = os.path.join(tmp_path, "updated_config.yaml")
    with open(updated_config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Initialize masker
    masker = Masker(updated_config_path)
    
    # Run masking
    masker.mask()
    
    # Verify output exists
    assert os.path.exists(output_path)
    
    # Load original and masked data
    original_data = pd.read_csv(sample_csv_file)
    masked_data = pd.read_csv(output_path)
    
    # Verify structure is preserved
    assert masked_data.shape == original_data.shape
    assert list(masked_data.columns) == list(original_data.columns)
    
    # Verify masking rules were applied correctly
    # customer_id should be preserved (passthrough)
    pd.testing.assert_series_equal(
        masked_data['customer_id'], 
        original_data['customer_id']
    )
    
    # first_name, last_name, email should be changed (faker)
    assert not masked_data['first_name'].equals(original_data['first_name'])
    assert not masked_data['last_name'].equals(original_data['last_name'])
    assert not masked_data['email'].equals(original_data['email'])
    
    # ssn should be hashed
    assert not masked_data['ssn'].equals(original_data['ssn'])
    assert all(len(x) == 64 for x in masked_data['ssn'])  # SHA-256 hashes are 64 chars
    
    # address should be redacted
    assert all(x == 'CONFIDENTIAL' for x in masked_data['address'])

def test_masker_fixed_width(sample_fixed_width_file, tmp_path, mock_logger):
    """Test masking with fixed-width input/output."""
    # Create config for fixed-width masking
    config_path = os.path.join(tmp_path, "fw_mask_config.yaml")
    output_path = os.path.join(tmp_path, "masked_fw.txt")
    output_def_path = os.path.join(tmp_path, "masked_fw.def")
    
    config = {
        'input': {
            'type': 'fixed_width',
            'path': sample_fixed_width_file['data_path'],
            'definition_path': sample_fixed_width_file['def_path'],
            'fixed_width_options': {
                'encoding': 'utf-8',
                'definition_delimiter': ','
            }
        },
        'output': {
            'type': 'fixed_width',
            'path': output_path,
            'definition_path': output_def_path,
            'fixed_width_options': {
                'encoding': 'utf-8'
            }
        },
        'global_settings': {
            'seed': 42
        },
        'masking_rules': [
            {'column': 'customer_id', 'type': 'passthrough'},
            {'column': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
            {'column': 'last_name', 'type': 'faker', 'faker_type': 'last_name'},
            {'column': 'email', 'type': 'faker', 'faker_type': 'email'},
            {'column': 'phone', 'type': 'redact', 'redact_with': 'PRIVATE'}
        ]
    }
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Initialize masker
    masker = Masker(config_path)
    
    # Run masking
    masker.mask()
    
    # Verify output exists
    assert os.path.exists(output_path)
    assert os.path.exists(output_def_path)
    
    # Load masked data using FixedWidthHandler
    from decoy_engine.connectors.fixed_width import FixedWidthHandler
    handler = FixedWidthHandler(
        {
            'type': 'fixed_width',
            'path': output_path,
            'definition_path': output_def_path,
            'fixed_width_options': {'encoding': 'utf-8'}
        },
        {'type': 'csv', 'path': 'dummy.csv'},
        mock_logger
    )
    
    masked_data = handler.load_data()
    
    # Load original data
    original_handler = FixedWidthHandler(
        {
            'type': 'fixed_width',
            'path': sample_fixed_width_file['data_path'],
            'definition_path': sample_fixed_width_file['def_path'],
            'fixed_width_options': {'encoding': 'utf-8'}
        },
        {'type': 'csv', 'path': 'dummy.csv'},
        mock_logger
    )
    
    original_data = original_handler.load_data()
    
    # Verify structure is preserved
    assert masked_data.shape == original_data.shape
    assert list(masked_data.columns) == list(original_data.columns)
    
    # Verify masking rules were applied correctly
    # customer_id should be preserved (passthrough)
    pd.testing.assert_series_equal(
        masked_data['customer_id'], 
        original_data['customer_id']
    )
    
    # first_name, last_name, email should be changed (faker)
    assert not masked_data['first_name'].equals(original_data['first_name'])
    assert not masked_data['last_name'].equals(original_data['last_name'])
    assert not masked_data['email'].equals(original_data['email'])
    
    # phone should be redacted
    assert all(x == 'PRIVATE' for x in masked_data['phone'])

def test_masker_referential_integrity(tmp_path, mock_logger):
    """Test masking with referential integrity."""
    # Create sample data with referential integrity
    customers_path = os.path.join(tmp_path, "customers.csv")
    orders_path = os.path.join(tmp_path, "orders.csv")
    
    # Create customers data
    customers = pd.DataFrame({
        'customer_id': ['C001', 'C002', 'C003'],
        'name': ['John Doe', 'Jane Smith', 'Bob Johnson'],
        'email': ['john@example.com', 'jane@example.com', 'bob@example.com']
    })
    customers.to_csv(customers_path, index=False)
    
    # Create orders data with references to customers
    orders = pd.DataFrame({
        'order_id': ['O001', 'O002', 'O003', 'O004', 'O005'],
        'customer_id': ['C001', 'C002', 'C001', 'C003', 'C002'],
        'amount': [100, 200, 150, 300, 250]
    })
    orders.to_csv(orders_path, index=False)
    
    # Create config for masking customers
    customers_config_path = os.path.join(tmp_path, "customers_mask_config.yaml")
    customers_output_path = os.path.join(tmp_path, "masked_customers.csv")
    legacy_state_path = os.path.join(tmp_path, "mappings")
    
    customers_config = {
        'input': {
            'type': 'csv',
            'path': customers_path,
            'csv_options': {'delimiter': ',', 'encoding': 'utf-8'}
        },
        'output': {
            'type': 'csv',
            'path': customers_output_path,
            'csv_options': {'delimiter': ',', 'encoding': 'utf-8'}
        },
        'global_settings': {
            'seed': 42
        },
        'masking_rules': [
            {'column': 'customer_id', 'type': 'hash'},
            {'column': 'name', 'type': 'faker', 'faker_type': 'name'},
            {'column': 'email', 'type': 'faker', 'faker_type': 'email'}
        ],
        'referential_integrity': [
            {
                'name': 'customer_relation',
                'columns': ['customers.customer_id', 'orders.customer_id']
            }
        ],
    }
    
    with open(customers_config_path, 'w') as f:
        yaml.dump(customers_config, f)
    
    # Create config for masking orders
    orders_config_path = os.path.join(tmp_path, "orders_mask_config.yaml")
    orders_output_path = os.path.join(tmp_path, "masked_orders.csv")
    
    orders_config = {
        'input': {
            'type': 'csv',
            'path': orders_path,
            'csv_options': {'delimiter': ',', 'encoding': 'utf-8'}
        },
        'output': {
            'type': 'csv',
            'path': orders_output_path,
            'csv_options': {'delimiter': ',', 'encoding': 'utf-8'}
        },
        'global_settings': {
            'seed': 42
        },
        'masking_rules': [
            {'column': 'order_id', 'type': 'hash'},
            {'column': 'customer_id', 'type': 'hash'},
            {'column': 'amount', 'type': 'passthrough'}
        ],
        'referential_integrity': [
            {
                'name': 'customer_relation',
                'columns': ['customers.customer_id', 'orders.customer_id']
            }
        ],
    }
    
    with open(orders_config_path, 'w') as f:
        yaml.dump(orders_config, f)
    
    # Mask customers first
    customers_masker = Masker(customers_config_path)
    customers_masker.mask()
    
    # Then mask orders
    orders_masker = Masker(orders_config_path)
    orders_masker.mask()
    
    # Verify outputs exist
    assert os.path.exists(customers_output_path)
    assert os.path.exists(orders_output_path)
    assert not os.path.exists(legacy_state_path)
    
    # Load masked data
    masked_customers = pd.read_csv(customers_output_path)
    masked_orders = pd.read_csv(orders_output_path)
    
    # Verify referential integrity is maintained
    # Create a lookup of original customer ID to masked customer ID.
    original_to_masked = {}
    for i, row in customers.iterrows():
        original_id = row['customer_id']
        masked_id = masked_customers.loc[i, 'customer_id']
        original_to_masked[original_id] = masked_id
    
    # Check each order's customer_id matches the expected masked value
    for i, row in orders.iterrows():
        original_id = row['customer_id']
        expected_masked_id = original_to_masked[original_id]
        actual_masked_id = masked_orders.loc[i, 'customer_id']
        assert actual_masked_id == expected_masked_id, \
            f"Referential integrity broken: Order {i} has customer_id {actual_masked_id} " \
            f"but should have {expected_masked_id}"
