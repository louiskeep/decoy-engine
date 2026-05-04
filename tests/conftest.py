# tests/conftest.py
"""
Common fixtures for genmask tests.
"""

import os
import shutil
import pytest
import pandas as pd
import yaml
from pathlib import Path

# Constants for test paths
TEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_DIR = os.path.join(TEST_DATA_DIR, "input")
OUTPUT_DIR = os.path.join(TEST_DATA_DIR, "output")
EXPECTED_DIR = os.path.join(TEST_DATA_DIR, "expected")
CONFIG_DIR = os.path.join(TEST_DATA_DIR, "config")
MAPPING_DIR = os.path.join(TEST_DATA_DIR, "mappings")

@pytest.fixture(scope="session", autouse=True)
def setup_test_directories():
    """Create necessary directories for tests."""
    for directory in [INPUT_DIR, OUTPUT_DIR, EXPECTED_DIR, CONFIG_DIR, MAPPING_DIR]:
        os.makedirs(directory, exist_ok=True)
    
    yield
    
    # Clean up output directory after tests
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR)

@pytest.fixture
def sample_csv_file():
    """Create a sample CSV file for testing."""
    csv_path = os.path.join(INPUT_DIR, "sample.csv")
    
    # Create a sample DataFrame
    data = {
        'customer_id': ['CUST001', 'CUST002', 'CUST003', 'CUST004', 'CUST005'],
        'first_name': ['John', 'Jane', 'Alice', 'Bob', 'Carol'],
        'last_name': ['Doe', 'Smith', 'Johnson', 'Brown', 'Williams'],
        'email': ['john.doe@example.com', 'jane.smith@example.com', 
                  'alice.j@example.com', 'bob.brown@example.com', 'carol.w@example.com'],
        'phone': ['555-1234', '555-5678', '555-9012', '555-3456', '555-7890'],
        'ssn': ['123-45-6789', '987-65-4321', '111-22-3333', '444-55-6666', '777-88-9999'],
        'address': ['123 Main St', '456 Oak Ave', '789 Pine Rd', '321 Elm Blvd', '654 Maple Dr']
    }
    
    df = pd.DataFrame(data)
    df.to_csv(csv_path, index=False)
    
    return csv_path

@pytest.fixture
def sample_fixed_width_file():
    """Create a sample fixed-width file for testing."""
    fw_path = os.path.join(INPUT_DIR, "sample_fw.txt")
    fw_def_path = os.path.join(INPUT_DIR, "sample_fw.def")
    
    # Create fixed-width definition file
    with open(fw_def_path, 'w') as f:
        f.write("FIELD,START,FINISH\n")
        f.write("customer_id,1,10\n")
        f.write("first_name,11,25\n")
        f.write("last_name,26,40\n")
        f.write("email,41,70\n")
        f.write("phone,71,80\n")
    
    # Create fixed-width data file
    with open(fw_path, 'w') as f:
        f.write("CUST001   John           Doe             john.doe@example.com            555-1234 \n")
        f.write("CUST002   Jane           Smith           jane.smith@example.com          555-5678 \n")
        f.write("CUST003   Alice          Johnson         alice.j@example.com             555-9012 \n")
        f.write("CUST004   Bob            Brown           bob.brown@example.com           555-3456 \n")
        f.write("CUST005   Carol          Williams        carol.w@example.com             555-7890 \n")
    
    return {'data_path': fw_path, 'def_path': fw_def_path}

@pytest.fixture
def sample_masking_config():
    """Create a sample masking configuration."""
    config_path = os.path.join(CONFIG_DIR, "sample_mask_config.yaml")
    
    config = {
        'input': {
            'type': 'csv',
            'path': '{INPUT_PATH}',
            'csv_options': {
                'delimiter': ',',
                'encoding': 'utf-8'
            }
        },
        'output': {
            'type': 'csv',
            'path': '{OUTPUT_PATH}',
            'csv_options': {
                'delimiter': ',',
                'encoding': 'utf-8',
                'quoting': 'minimal'
            }
        },
        'global_settings': {
            'seed': 42,
            'chunk_size': 1000,
        },
        'logging': {
            'level': 'info',
            'file': os.path.join(OUTPUT_DIR, 'test_log.log'),
            'console': True,
            'verbose': False
        },
        'masking_rules': [
            {'column': 'customer_id', 'type': 'passthrough'},
            {'column': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
            {'column': 'last_name', 'type': 'faker', 'faker_type': 'last_name'},
            {'column': 'email', 'type': 'faker', 'faker_type': 'email', 'preserve_domain': True},
            {'column': 'phone', 'type': 'faker', 'faker_type': 'phone_number'},
            {'column': 'ssn', 'type': 'hash'},
            {'column': 'address', 'type': 'redact', 'redact_with': 'CONFIDENTIAL'}
        ]
    }
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    return config_path

@pytest.fixture
def sample_generator_config():
    """Create a sample data generator configuration."""
    config_path = os.path.join(CONFIG_DIR, "sample_gen_config.yaml")
    
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': OUTPUT_DIR,
            'chunk_size': 1000
        },
        'tables': [
            {
                'name': 'customers',
                'rows': 10,
                'output_path': os.path.join(OUTPUT_DIR, 'generated_customers.csv'),
                'columns': [
                    {'name': 'customer_id', 'type': 'sequence', 'start': 1000, 'prefix': 'CUST', 'pad_length': 6},
                    {'name': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
                    {'name': 'last_name', 'type': 'faker', 'faker_type': 'last_name'},
                    {'name': 'email', 'type': 'formula', 'formula_type': 'template', 
                     'formula': '{first_name.lower()}.{last_name.lower()}@example.com'},
                    {'name': 'status', 'type': 'categorical', 'categories': ['Active', 'Inactive', 'Pending'],
                     'weights': [0.7, 0.2, 0.1]}
                ]
            },
            {
                'name': 'orders',
                'rows': 20,
                'output_path': os.path.join(OUTPUT_DIR, 'generated_orders.csv'),
                'columns': [
                    {'name': 'order_id', 'type': 'sequence', 'start': 5000, 'prefix': 'ORD', 'pad_length': 6},
                    {'name': 'customer_id', 'type': 'reference', 'reference_table': 'customers', 
                     'reference_column': 'customer_id'},
                    {'name': 'order_date', 'type': 'faker', 'faker_type': 'date'},
                    {'name': 'amount', 'type': 'formula', 'formula_type': 'basic', 
                     'formula': 'round(randint(1000, 50000) / 100, 2)'}
                ]
            }
        ],
        'relationships': [
            {
                'name': 'customer_orders',
                'type': 'foreign_key',
                'source_table': 'orders',
                'source_column': 'customer_id',
                'target_table': 'customers', 
                'target_column': 'customer_id'
            }
        ]
    }
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    return config_path

@pytest.fixture
def mock_logger():
    """Create a mock logger for testing."""
    from forge_engine.utils.logging import get_logger
    
    logger_config = {
        'level': 'debug',
        'console': True,
        'verbose': True
    }
    
    return get_logger(logger_config)