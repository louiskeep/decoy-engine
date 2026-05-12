# decoy_engine/masker/processor.py
"""
Data processing logic for the decoy_engine masker.
Handles the application of masking rules to dataframes and chunks.
"""

import pandas as pd
from typing import Dict, Any, List

from decoy_engine.transforms.format_preservation import apply_format_preservation

class MaskingProcessor:
    """
    Handles the application of masking rules to data.
    Manages referential integrity and strategy application.
    """
    
    def __init__(self, config: Dict[str, Any], strategy_manager, ref_integrity, logger=None):
        """
        Initialize the processor with required components
        
        Args:
            config: Configuration dictionary
            strategy_manager: StrategyManager instance
            ref_integrity: ReferentialIntegrityManager instance
            logger: Logger instance (optional)
        """
        self.config = config
        self.strategy_manager = strategy_manager
        self.ref_integrity = ref_integrity
        
        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger
            self.logger = get_logger()
    
    def apply_masking_rules(self, df: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """
        Apply masking rules to the entire DataFrame
        
        Args:
            df: pandas DataFrame to mask
            table_name: Name of the table
            
        Returns:
            Masked pandas DataFrame
        """
        # Check if all masking rules have matching columns
        missing_columns = []
        for rule in self.config['masking_rules']:
            column = rule['column']
            if column not in df.columns:
                missing_columns.append(column)
        
        if missing_columns:
            self.logger.warning(f"The following columns from masking rules were not found in the data: {', '.join(missing_columns)}")
            self.logger.warning("Available columns: " + ", ".join(df.columns.tolist()))
        
        # Apply masking rules
        rule_count = len(self.config['masking_rules'])
        self.logger.info(f"Applying {rule_count} masking rules")

        from decoy_engine.internal.memory import MemoryMonitor
        MemoryMonitor.monitor_memory_usage(self.logger, "Before applying masking rules")
        
        for i, rule in enumerate(self.config['masking_rules'], 1):
            column = rule['column']
            
            # Skip if column doesn't exist in the dataframe
            if column not in df.columns:
                self.logger.warning(f"Column '{column}' not found in input data. Skipping.")
                continue
            
            self.logger.info(f"[{i}/{rule_count}] Applying mask to column '{column}' with rule type '{rule['type']}'")
            
            # Check for nulls in the column
            null_count = df[column].isna().sum()
            if null_count > 0:
                null_percent = (null_count / len(df)) * 100
                self.logger.debug(f"Column '{column}' contains {null_count} null values ({null_percent:.1f}%)")
            
            # Check if column is part of referential integrity relationship
            rel_name = self.ref_integrity.get_referential_relationship(table_name, column)
            
            # Capture the source column up front so the format-preservation
            # post-pass (Item 65) can re-shape the masked output to match
            # the source's surface format. Cheap shallow copy; we don't
            # mutate `source` after this point.
            source = df[column].copy()

            if rel_name:
                # Apply masking with referential integrity
                self.logger.info(f"Column '{column}' is part of relationship '{rel_name}'. Applying global mapping.")
                df[column] = self.ref_integrity.apply_global_mapping(df[column], rel_name, rule)
            else:
                # Apply regular masking, with optional row-level conditions
                conds = rule.get('conditions')
                if conds:
                    row_mask = self._evaluate_conditions(df, conds, rule.get('condition_logic', 'AND'))
                    original = df[column].copy()
                    masked   = self.strategy_manager.apply_masking_rule(df[column], rule)
                    df[column] = original.where(~row_mask, masked)
                else:
                    df[column] = self.strategy_manager.apply_masking_rule(df[column], rule)

            # Item 65 — format-preservation post-pass. No-op unless the
            # rule sets preserve_format=true; opt-out by strategy is
            # handled inside apply_format_preservation (hash, redact,
            # passthrough, date_shift all skip).
            if rule.get('preserve_format'):
                df[column] = apply_format_preservation(source, df[column], rule)

        MemoryMonitor.monitor_memory_usage(self.logger, "After applying masking rules")

        return df

    def _evaluate_conditions(
        self, df: pd.DataFrame, conditions: list, logic: str = 'AND'
    ) -> pd.Series:
        """Return a boolean Series: True = this row should be masked."""
        VALID_OPS = {
            'eq', 'ne', 'gt', 'gte', 'lt', 'lte',
            'in', 'not_in', 'contains', 'not_contains',
            'is_null', 'is_not_null',
        }
        masks = []
        for cond in conditions:
            col = cond.get('column', '')
            op  = cond.get('operator', 'eq')
            val = cond.get('value', '')
            if col not in df.columns:
                self.logger.warning(f"Condition column '{col}' not found in data â€” condition skipped")
                continue
            if op not in VALID_OPS:
                self.logger.warning(f"Unknown condition operator '{op}' â€” condition skipped")
                continue
            s = df[col]
            if   op == 'eq':           m = s == val
            elif op == 'ne':           m = s != val
            elif op == 'gt':           m = pd.to_numeric(s, errors='coerce') > float(val)
            elif op == 'gte':          m = pd.to_numeric(s, errors='coerce') >= float(val)
            elif op == 'lt':           m = pd.to_numeric(s, errors='coerce') < float(val)
            elif op == 'lte':          m = pd.to_numeric(s, errors='coerce') <= float(val)
            elif op == 'in':
                items = val if isinstance(val, list) else [v.strip() for v in str(val).split(',')]
                m = s.isin(items)
            elif op == 'not_in':
                items = val if isinstance(val, list) else [v.strip() for v in str(val).split(',')]
                m = ~s.isin(items)
            elif op == 'contains':     m = s.astype(str).str.contains(str(val), na=False)
            elif op == 'not_contains': m = ~s.astype(str).str.contains(str(val), na=False)
            elif op == 'is_null':      m = s.isna()
            else:                      m = s.notna()  # is_not_null
            masks.append(m)

        if not masks:
            return pd.Series([True] * len(df), index=df.index)
        combined = masks[0]
        for m in masks[1:]:
            combined = (combined & m) if logic.upper() == 'AND' else (combined | m)
        return combined

    def apply_masking_rules_to_chunk(self, chunk: pd.DataFrame, table_name: str) -> pd.DataFrame:
        """
        Apply masking rules to a chunk of data
        
        Args:
            chunk: Chunk of data to mask
            table_name: Name of the table
            
        Returns:
            Masked chunk
        """
        return self.apply_masking_rules(chunk, table_name)