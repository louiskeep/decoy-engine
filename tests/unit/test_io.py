# tests/unit/test_io.py
"""
Unit tests for IO handlers.
"""

import pytest
import pandas as pd
import os

from forge_engine.io.csv import CSVHandler
from forge_engine.io.fixed_width import FixedWidthHandler
from forge_engine.io.factory import create_io_handler

def test_csv_handler(sample_csv_file, mock_logger, tmp_path):
    """Test CSV handler."""
    output_path = os.path.join(tmp_path, "output.csv")
    
    # Create input and output configs
    input_config = {
        'type': 'csv',
        'path': sample_csv_file,
        'csv_options': {
            'delimiter': ',',
            'encoding': 'utf-8'
        }
    }
    
    output_config = {
        'type': 'csv',
        'path': output_path,
        'csv_options': {
            'delimiter': ',',
            'encoding': 'utf-8',
            'quoting': 'minimal'
        }
    }
    
    # Create handler
    handler = CSVHandler(input_config, output_config, mock_logger)
    
    # Test loading data
    df = handler.load_data()
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert 'customer_id' in df.columns
    assert 'first_name' in df.columns
    
    # Test saving data
    handler.save_data(df)
    
    # Verify the saved file exists and can be loaded
    assert os.path.exists(output_path)
    df_loaded = pd.read_csv(output_path)
    pd.testing.assert_frame_equal(df_loaded, df)
    
    # Test sample loading
    df_sample = handler.load_sample(2)
    assert len(df_sample) == 2
    assert list(df_sample.columns) == list(df.columns)

def test_fixed_width_handler(sample_fixed_width_file, mock_logger, tmp_path):
    """Test fixed-width handler."""
    output_path = os.path.join(tmp_path, "output_fw.txt")
    
    # Create input and output configs
    input_config = {
        'type': 'fixed_width',
        'path': sample_fixed_width_file['data_path'],
        'definition_path': sample_fixed_width_file['def_path'],
        'fixed_width_options': {
            'encoding': 'utf-8',
            'definition_delimiter': ','
        }
    }
    
    # Test with CSV output (conversion)
    output_config_csv = {
        'type': 'csv',
        'path': os.path.join(tmp_path, "output_fw_as_csv.csv"),
        'csv_options': {
            'delimiter': ',',
            'encoding': 'utf-8'
        }
    }
    
    # Create handler for CSV output
    handler_csv_out = FixedWidthHandler(input_config, output_config_csv, mock_logger)
    
    # Test loading data
    df = handler_csv_out.load_data()
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert 'customer_id' in df.columns
    assert 'first_name' in df.columns
    
    # Test saving as CSV
    handler_csv_out.save_data(df)
    
    # Verify the saved CSV file exists and can be loaded
    assert os.path.exists(output_config_csv['path'])
    df_loaded_csv = pd.read_csv(output_config_csv['path'])
    pd.testing.assert_frame_equal(df_loaded_csv, df)
    
    # Test fixed-width to fixed-width with the same definition
    output_config_fw = {
        'type': 'fixed_width',
        'path': output_path,
        'definition_path': sample_fixed_width_file['def_path'],
        'fixed_width_options': {
            'encoding': 'utf-8'
        }
    }
    
    # Create handler for fixed-width output
    handler_fw_out = FixedWidthHandler(input_config, output_config_fw, mock_logger)
    
    # Test saving as fixed-width
    handler_fw_out.save_data(df)
    
    # Verify the saved fixed-width file exists and can be loaded
    assert os.path.exists(output_path)
    
    # Load the saved fixed-width file
    verification_handler = FixedWidthHandler(
        {'type': 'fixed_width', 'path': output_path, 'definition_path': sample_fixed_width_file['def_path']},
        {'type': 'csv', 'path': 'dummy.csv'},
        mock_logger
    )
    df_loaded_fw = verification_handler.load_data()
    
    # Compare loaded data with original
    pd.testing.assert_frame_equal(df_loaded_fw, df)

def test_io_handler_factory(sample_csv_file, sample_fixed_width_file, mock_logger):
    """Test IO handler factory."""
    # CSV handler
    input_config_csv = {
        'type': 'csv',
        'path': sample_csv_file
    }
    
    handler_csv = create_io_handler(input_config_csv, {'type': 'csv', 'path': 'dummy.csv'}, mock_logger)
    assert isinstance(handler_csv, CSVHandler)
    
    # Fixed-width handler
    input_config_fw = {
        'type': 'fixed_width',
        'path': sample_fixed_width_file['data_path'],
        'definition_path': sample_fixed_width_file['def_path']
    }
    
    handler_fw = create_io_handler(input_config_fw, {'type': 'csv', 'path': 'dummy.csv'}, mock_logger)
    assert isinstance(handler_fw, FixedWidthHandler)
    
    # Invalid type
    input_config_invalid = {
        'type': 'invalid',
        'path': 'invalid.txt'
    }
    
    with pytest.raises(ValueError):
        create_io_handler(input_config_invalid, {'type': 'csv', 'path': 'dummy.csv'}, mock_logger)