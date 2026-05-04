# tests/unit/test_integrity.py
"""
Unit tests for referential integrity handling.
"""

import pytest
import pandas as pd
import os
import tempfile
import shutil

from forge_engine.internal.integrity import ReferentialIntegrityManager

@pytest.fixture
def related_dataframes():
    """Create related dataframes for testing referential integrity."""
    # Customer data
    customers = pd.DataFrame({
        'customer_id': ['CUST001', 'CUST002', 'CUST003'],
        'name': ['John Doe', 'Jane Smith', 'Alice Johnson']
    })
    
    # Order data with references to customers
    orders = pd.DataFrame({
        'order_id': ['ORD001', 'ORD002', 'ORD003', 'ORD004'],
        'customer_id': ['CUST001', 'CUST002', 'CUST001', 'CUST003'],
        'amount': [100.0, 200.0, 150.0, 300.0]
    })
    
    # Payment data with references to orders
    payments = pd.DataFrame({
        'payment_id': ['PAY001', 'PAY002', 'PAY003', 'PAY004'],
        'order_id': ['ORD001', 'ORD002', 'ORD003', 'ORD004'],
        'amount': [100.0, 200.0, 150.0, 300.0]
    })
    
    return {'customers': customers, 'orders': orders, 'payments': payments}

def test_referential_integrity(related_dataframes, mock_logger):
    """Test referential integrity manager."""
    # Create temporary directory for mappings
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Create config with referential integrity relations
        config = {
            'referential_integrity': [
                {
                    'name': 'customer_relation',
                    'columns': ['customers.customer_id', 'orders.customer_id']
                },
                {
                    'name': 'order_relation',
                    'columns': ['orders.order_id', 'payments.order_id']
                }
            ],
            'mappings': {
                'store_directory': temp_dir
            }
        }
        
        # Initialize manager
        manager = ReferentialIntegrityManager(config, mock_logger)
        
        # Test get_referential_relationship
        assert manager.get_referential_relationship('customers', 'customer_id') == 'customer_relation'
        assert manager.get_referential_relationship('orders', 'customer_id') == 'customer_relation'
        assert manager.get_referential_relationship('orders', 'order_id') == 'order_relation'
        assert manager.get_referential_relationship('payments', 'order_id') == 'order_relation'
        assert manager.get_referential_relationship('customers', 'name') is None
        
        # Test applying global mapping
        # First to the customers table
        rule = {'column': 'customer_id', 'type': 'hash'}
        customers_column = related_dataframes['customers']['customer_id']
        masked_customers = manager.apply_global_mapping(customers_column, 'customer_relation', rule)
        
        # Ensure all values changed
        assert not customers_column.equals(masked_customers)
        
        # Apply to orders table - should use the same mapping
        orders_column = related_dataframes['orders']['customer_id']
        masked_orders = manager.apply_global_mapping(orders_column, 'customer_relation', rule)
        
        # Verify referential integrity is maintained
        # For each order, get the original customer_id
        for i, original_id in enumerate(orders_column):
            # Find the index in the customers dataframe
            customer_idx = customers_column[customers_column == original_id].index[0]
            
            # The masked order customer_id should match the masked customer's customer_id
            assert masked_orders[i] == masked_customers[customer_idx]
        
        # Test saving mappings
        manager.save_global_mappings()
        
        # Verify mapping files were created
        assert os.path.exists(os.path.join(temp_dir, "global_customer_relation_map.json"))
        assert os.path.exists(os.path.join(temp_dir, "global_order_relation_map.json"))
        
        # Create a new manager to load from the saved mappings
        new_manager = ReferentialIntegrityManager(config, mock_logger)
        
        # Apply mapping with the new manager
        new_masked_customers = new_manager.apply_global_mapping(customers_column, 'customer_relation', rule)
        
        # Verify it uses the same mapping
        pd.testing.assert_series_equal(masked_customers, new_masked_customers)
    
    finally:
        # Clean up
        shutil.rmtree(temp_dir)