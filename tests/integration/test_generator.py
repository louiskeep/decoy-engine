# tests/integration/test_generator.py
"""
Integration tests for the DataGenerator class.
"""

import pytest
import pandas as pd
import os
import yaml

from decoy_engine.generators import DataGenerator

def test_generator_integration(sample_generator_config, tmp_path, mock_logger):
    """Test the complete data generation process."""
    # Load and update config paths
    with open(sample_generator_config, 'r') as f:
        config = yaml.safe_load(f)
    
    # Update output paths
    config['generator_settings']['output_directory'] = str(tmp_path)
    for table in config['tables']:
        table['output_path'] = os.path.join(tmp_path, f"{table['name']}.csv")
    
    # Save updated config
    updated_config_path = os.path.join(tmp_path, "updated_gen_config.yaml")
    with open(updated_config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Initialize generator
    generator = DataGenerator(updated_config_path, mock_logger)
    
    # Run generation
    generator.generate()
    
    # Verify outputs exist
    assert os.path.exists(os.path.join(tmp_path, "customers.csv"))
    assert os.path.exists(os.path.join(tmp_path, "orders.csv"))
    
    # Load generated data
    customers = pd.read_csv(os.path.join(tmp_path, "customers.csv"))
    orders = pd.read_csv(os.path.join(tmp_path, "orders.csv"))
    
    # Verify row counts
    assert len(customers) == 10  # Should match config
    assert len(orders) == 20  # Should match config
    
    # Verify column structure
    assert 'customer_id' in customers.columns
    assert 'first_name' in customers.columns
    assert 'last_name' in customers.columns
    assert 'email' in customers.columns
    assert 'status' in customers.columns
    
    assert 'order_id' in orders.columns
    assert 'customer_id' in orders.columns
    assert 'order_date' in orders.columns
    assert 'amount' in orders.columns
    
    # Verify referential integrity
    # All order.customer_id values should exist in customers.customer_id
    assert set(orders['customer_id']).issubset(set(customers['customer_id']))
    
    # Verify deterministic behavior
    # Run generation again with the same config
    generator2 = DataGenerator(updated_config_path, mock_logger)
    
    # Create a different output directory
    repeat_dir = os.path.join(tmp_path, "repeat")
    os.makedirs(repeat_dir, exist_ok=True)
    
    # Update config to use the new directory
    with open(updated_config_path, 'r') as f:
        repeat_config = yaml.safe_load(f)
    
    repeat_config['generator_settings']['output_directory'] = repeat_dir
    for table in repeat_config['tables']:
        table['output_path'] = os.path.join(repeat_dir, f"{table['name']}.csv")
    
    # Save updated config
    repeat_config_path = os.path.join(tmp_path, "repeat_config.yaml")
    with open(repeat_config_path, 'w') as f:
        yaml.dump(repeat_config, f)
    
    # Run generation with the same seed
    repeat_generator = DataGenerator(repeat_config_path, mock_logger)
    repeat_generator.generate()
    
    # Load the repeated generation
    repeat_customers = pd.read_csv(os.path.join(repeat_dir, "customers.csv"))
    repeat_orders = pd.read_csv(os.path.join(repeat_dir, "orders.csv"))
    
    # Verify deterministic output (should be identical)
    pd.testing.assert_frame_equal(customers, repeat_customers)
    pd.testing.assert_frame_equal(orders, repeat_orders)
    
    # Test with a different seed to verify different output
    with open(updated_config_path, 'r') as f:
        different_seed_config = yaml.safe_load(f)
    
    different_seed_dir = os.path.join(tmp_path, "different_seed")
    os.makedirs(different_seed_dir, exist_ok=True)
    
    different_seed_config['generator_settings']['output_directory'] = different_seed_dir
    different_seed_config['generator_settings']['seed'] = 99  # Different seed
    for table in different_seed_config['tables']:
        table['output_path'] = os.path.join(different_seed_dir, f"{table['name']}.csv")
    
    # Save updated config
    different_seed_config_path = os.path.join(tmp_path, "different_seed_config.yaml")
    with open(different_seed_config_path, 'w') as f:
        yaml.dump(different_seed_config, f)
    
    # Run generation with different seed
    different_seed_generator = DataGenerator(different_seed_config_path, mock_logger)
    different_seed_generator.generate()
    
    # Load the generation with different seed
    different_seed_customers = pd.read_csv(os.path.join(different_seed_dir, "customers.csv"))
    
    # Output should be different with different seed
    assert not customers.equals(different_seed_customers)


def test_generator_composite_formula(tmp_path, mock_logger):
    """Test composite formula processing in generated output."""
    output_path = os.path.join(tmp_path, "people.csv")
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': str(tmp_path)
        },
        'tables': [
            {
                'name': 'people',
                'rows': 5,
                'output_path': output_path,
                'columns': [
                    {'name': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
                    {'name': 'last_name', 'type': 'faker', 'faker_type': 'last_name'},
                    {
                        # F0: every formula is a Python expression. To read
                        # sibling columns, declare them in `references` and
                        # write an f-string yourself. Pre-F0 this column
                        # used `formula_type: template`, which silently
                        # populated faker provider values into scope —
                        # confusing because the f-string LOOKED like it
                        # referenced the same row's columns when it
                        # actually referenced fresh per-row faker calls.
                        'name': 'email',
                        'type': 'formula',
                        'references': ['first_name', 'last_name'],
                        'formula': "f'{first_name.lower()}.{last_name.lower()}@example.com'",
                    },
                    {
                        'name': 'username',
                        'type': 'formula',
                        'references': ['first_name', 'last_name'],
                        'formula': "f'{first_name.lower()}_{last_name.lower()}'",
                    }
                ]
            }
        ]
    }

    config_path = os.path.join(tmp_path, "composite_formula_config.yaml")
    with open(config_path, 'w') as f:
        yaml.dump(config, f)

    generator = DataGenerator(config_path, mock_logger)
    generator.generate()

    generated = pd.read_csv(output_path)
    assert 'username' in generated.columns
    expected_usernames = generated['first_name'].str.lower() + '_' + generated['last_name'].str.lower()
    assert generated['username'].tolist() == expected_usernames.tolist()


def test_generator_relationships(tmp_path, mock_logger):
    """Test foreign key relationships and validation."""
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': str(tmp_path)
        },
        'tables': [
            {
                'name': 'departments',
                'rows': 3,
                'output_path': str(tmp_path / 'departments.csv'),
                'columns': [
                    {'name': 'dept_id', 'type': 'sequence', 'start': 100},
                    {'name': 'dept_name', 'type': 'categorical', 'categories': ['Engineering', 'Sales', 'HR']}
                ]
            },
            {
                'name': 'employees',
                'rows': 5,
                'output_path': str(tmp_path / 'employees.csv'),
                'columns': [
                    {'name': 'emp_id', 'type': 'sequence', 'start': 1},
                    {'name': 'emp_name', 'type': 'faker', 'faker_type': 'name'},
                    {'name': 'dept_id', 'type': 'reference', 'reference_table': 'departments', 'reference_column': 'dept_id'}
                ]
            }
        ],
        'relationships': [
            {
                'name': 'emp_dept_fk',
                'type': 'foreign_key',
                'source_table': 'employees',
                'source_column': 'dept_id',
                'target_table': 'departments',
                'target_column': 'dept_id',
                'distribution': 'weighted',
                'weights': [0.5, 0.3, 0.2]  # Valid weights that sum to 1.0
            }
        ]
    }

    config_path = tmp_path / "relationships_config.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(config, f)

    generator = DataGenerator(str(config_path), mock_logger)
    generator.generate()

    # Verify departments were created
    dept_df = pd.read_csv(tmp_path / 'departments.csv')
    assert len(dept_df) == 3
    assert set(dept_df['dept_id'].tolist()) == {100, 101, 102}

    # Verify employees were created with valid department references
    emp_df = pd.read_csv(tmp_path / 'employees.csv')
    assert len(emp_df) == 5
    assert all(emp_df['dept_id'].isin([100, 101, 102]))


def test_generator_invalid_weights_warning(tmp_path, mock_logger, caplog):
    """Test that invalid weights trigger a warning and fall back to equal weights."""
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': str(tmp_path)
        },
        'tables': [
            {
                'name': 'departments',
                'rows': 2,
                'output_path': str(tmp_path / 'departments.csv'),
                'columns': [
                    {'name': 'dept_id', 'type': 'sequence', 'start': 100},
                    {'name': 'dept_name', 'type': 'categorical', 'categories': ['Engineering', 'Sales']}
                ]
            },
            {
                'name': 'employees',
                'rows': 3,
                'output_path': str(tmp_path / 'employees.csv'),
                'columns': [
                    {'name': 'emp_id', 'type': 'sequence', 'start': 1},
                    {'name': 'emp_name', 'type': 'faker', 'faker_type': 'name'},
                    {'name': 'dept_id', 'type': 'reference', 'reference_table': 'departments', 'reference_column': 'dept_id'}
                ]
            }
        ],
        'relationships': [
            {
                'name': 'emp_dept_fk',
                'type': 'foreign_key',
                'source_table': 'employees',
                'source_column': 'dept_id',
                'target_table': 'departments',
                'target_column': 'dept_id',
                'distribution': 'weighted',
                'weights': [0.8, 0.5]  # Invalid weights that don't sum to 1.0
            }
        ]
    }

    config_path = tmp_path / "invalid_weights_config.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(config, f)

    generator = DataGenerator(str(config_path), mock_logger)
    generator.generate()

    # Check that warning was logged
    assert "Weights for relationship 'emp_dept_fk' do not sum to 1.0" in caplog.text

    # Verify generation still worked (fell back to equal weights)
    emp_df = pd.read_csv(tmp_path / 'employees.csv')
    assert len(emp_df) == 3
    assert all(emp_df['dept_id'].isin([100, 101]))


def test_generator_dependency_order_validation(tmp_path, mock_logger):
    """Test that dependency order validation catches missing tables."""
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': str(tmp_path)
        },
        'tables': [
            # Note: employees table comes first, but references departments which isn't generated yet
            {
                'name': 'employees',
                'rows': 3,
                'output_path': str(tmp_path / 'employees.csv'),
                'columns': [
                    {'name': 'emp_id', 'type': 'sequence', 'start': 1},
                    {'name': 'emp_name', 'type': 'faker', 'faker_type': 'name'}
                ]
            }
        ],
        'relationships': [
            {
                'name': 'invalid_fk',
                'type': 'foreign_key',
                'source_table': 'employees',
                'source_column': 'dept_id',
                'target_table': 'nonexistent_departments',  # This table doesn't exist
                'target_column': 'dept_id'
            }
        ]
    }

    config_path = tmp_path / "dependency_order_config.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(config, f)

    generator = DataGenerator(str(config_path), mock_logger)

    # Should raise ValueError due to missing target table
    with pytest.raises(ValueError, match="Relationship validation failed.*nonexistent_departments"):
        generator.generate()


def test_generator_fixed_width(tmp_path, mock_logger):
    """Test data generation with fixed-width output."""
    # Create definition file
    def_path = os.path.join(tmp_path, "test_fw.def")
    with open(def_path, 'w') as f:
        f.write("FIELD,START,FINISH\n")
        f.write("employee_id,1,10\n")
        f.write("first_name,11,25\n")
        f.write("last_name,26,40\n")
        f.write("department,41,60\n")
    
    # Create config
    config_path = os.path.join(tmp_path, "fw_gen_config.yaml")
    output_path = os.path.join(tmp_path, "employees.txt")
    
    config = {
        'generator_settings': {
            'seed': 42,
            'output_directory': str(tmp_path)
        },
        'tables': [
            {
                'name': 'employees',
                'rows': 10,
                'output_path': output_path,
                'output_type': 'fixed_width',
                'fixed_width_options': {
                    'encoding': 'utf-8',
                    'definition_path': def_path
                },
                'columns': [
                    {'name': 'employee_id', 'type': 'sequence', 'start': 1000, 'prefix': 'EMP'},
                    {'name': 'first_name', 'type': 'faker', 'faker_type': 'first_name'},
                    {'name': 'last_name', 'type': 'faker', 'faker_type': 'last_name'},
                    {'name': 'department', 'type': 'categorical', 
                     'categories': ['HR', 'IT', 'Finance', 'Marketing', 'Sales'],
                     'weights': [0.2, 0.2, 0.2, 0.2, 0.2]}
                ]
            }
        ]
    }
    
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Initialize generator
    generator = DataGenerator(config_path, mock_logger)
    
    # Run generation
    generator.generate()
    
    # Verify output exists
    assert os.path.exists(output_path)
    
    # Load generated data using FixedWidthHandler
    from decoy_engine.connectors.fixed_width import FixedWidthHandler
    handler = FixedWidthHandler(
        {
            'type': 'fixed_width',
            'path': output_path,
            'definition_path': def_path,
            'fixed_width_options': {'encoding': 'utf-8'}
        },
        {'type': 'csv', 'path': 'dummy.csv'},
        mock_logger
    )
    
    generated_data = handler.load_data()
    
    # Verify structure
    assert len(generated_data) == 10
    assert 'employee_id' in generated_data.columns
    assert 'first_name' in generated_data.columns
    assert 'last_name' in generated_data.columns
    assert 'department' in generated_data.columns
    
    # Verify employee_id format
    assert all(str(x).startswith('EMP') for x in generated_data['employee_id'])
    
    # Verify department values
    assert set(generated_data['department']).issubset(
        {'HR', 'IT', 'Finance', 'Marketing', 'Sales'}
    )