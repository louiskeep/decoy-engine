# decoy_engine/utils/processor.py
"""
Large file processor for the decoy_engine package.
Handles processing large files in chunks to minimize memory usage.
"""

import os
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from decoy_engine.internal.memory import MemoryMonitor


class LargeFileProcessor:
    """
    Handles processing of large files in chunks to minimize memory usage.
    """

    def __init__(self, config: dict[str, Any], logger=None):
        """
        Initialize with configuration and logger

        Args:
            config: Dictionary with configuration
            logger: Logger instance
        """
        self.config = config

        # Use provided logger or create a default one
        if logger:
            self.logger = logger
        else:
            from decoy_engine.internal.logging import get_logger

            self.logger = get_logger()

    def process_large_dataset(
        self,
        input_path: str,
        df_schema: pd.DataFrame,
        processor_func: Callable,
        output_path: str = None,
        description: str = "Processing",
    ):
        """
        Process a large dataset in chunks to minimize memory usage

        Args:
            input_path: Path to the input CSV file
            df_schema: DataFrame with the schema structure (empty or sample)
            processor_func: Function to process each chunk (takes chunk as input, returns processed chunk)
            output_path: Path to the output file (if None, no output is written)
            description: Description of the processing operation for logs

        Returns:
            Total number of rows processed
        """
        MemoryMonitor.monitor_memory_usage(self.logger, f"Before {description}")

        # Get CSV options from config
        input_options = self.config["input"].get("csv_options", {})
        delimiter = input_options.get("delimiter", ",")
        encoding = input_options.get("encoding", "utf-8")

        self.logger.debug(f"Input options: delimiter='{delimiter}', encoding='{encoding}'")

        # Create output path and directory if output is needed
        if output_path:
            from decoy_engine.internal.helpers import create_directory_for_file

            create_directory_for_file(output_path)
            self.logger.debug(f"Created output directory: {os.path.dirname(output_path)}")

            # Get output CSV options
            output_options = self.config["output"].get("csv_options", {})
            out_delimiter = output_options.get("delimiter", ",")
            out_encoding = output_options.get("encoding", "utf-8")
            quoting_mode = output_options.get("quoting", "minimal")

            from decoy_engine.internal.helpers import convert_quoting_mode

            quoting = convert_quoting_mode(quoting_mode)

            self.logger.debug(
                f"Output options: delimiter='{out_delimiter}', encoding='{out_encoding}', quoting='{quoting_mode}'"
            )

            # Write header to output file
            self.logger.info(f"Writing headers to output file: {output_path}")
            df_schema.to_csv(
                output_path, index=False, sep=out_delimiter, encoding=out_encoding, quoting=quoting
            )

        # Process in chunks
        chunk_size = self.config.get("global_settings", {}).get("chunk_size", 100000)
        total_rows_processed = 0
        start_time = time.time()

        self.logger.info(f"Processing with chunk size: {chunk_size:,} rows")

        # Count total rows for progress reporting
        try:
            self.logger.debug(
                "Counting total rows in file (this may take a moment for large files)"
            )
            count_start = time.time()
            total_rows = sum(1 for _ in open(input_path, encoding=encoding))
            total_rows = total_rows - 1  # Subtract header row
            count_time = time.time() - count_start

            self.logger.info(
                f"File contains {total_rows:,} rows (counted in {count_time:.2f} seconds)"
            )
        except Exception as e:
            self.logger.warning(f"Could not count total rows: {e}")
            self.logger.warning("Progress reporting will show rows processed without percentage")
            total_rows = None

        # Process chunks
        chunk_count = 0

        # Create progress logger
        from decoy_engine.internal.logging import ProgressLogger

        progress = ProgressLogger(self.logger, total_rows or 0, description)
        progress.start()

        for chunk_num, chunk in enumerate(
            pd.read_csv(input_path, chunksize=chunk_size, delimiter=delimiter, encoding=encoding)
        ):
            chunk_count += 1
            chunk_start_time = time.time()

            self.logger.info(f"Processing chunk {chunk_num + 1} ({len(chunk):,} rows)")

            if chunk_num % 10 == 0:  # Only log every 10 chunks to avoid excessive logging
                MemoryMonitor.monitor_memory_usage(
                    self.logger, f"Before processing chunk {chunk_num + 1}"
                )

            # Process the chunk
            processed_chunk = processor_func(chunk)

            # Append to output file if output path is provided
            if output_path and processed_chunk is not None:
                self.logger.debug(f"Appending chunk {chunk_num + 1} to output file")
                processed_chunk.to_csv(
                    output_path,
                    mode="a",
                    header=False,
                    index=False,
                    sep=out_delimiter,
                    encoding=out_encoding,
                    quoting=quoting,
                )

            # Update progress
            total_rows_processed += len(chunk)
            chunk_time = time.time() - chunk_start_time

            # Update progress logger
            progress.update(len(chunk))

            # Calculate and report chunk processing speed
            rows_per_second = len(chunk) / chunk_time if chunk_time > 0 else 0

            if total_rows:
                progress_percent = (total_rows_processed / total_rows) * 100
                eta_seconds = (
                    (total_rows - total_rows_processed) / rows_per_second
                    if rows_per_second > 0
                    else 0
                )

                # Format ETA nicely
                from decoy_engine.internal.helpers import format_elapsed_time

                eta_str = format_elapsed_time(eta_seconds)

                self.logger.debug(
                    f"Progress: {progress_percent:.1f}% complete - {rows_per_second:.0f} rows/sec - ETA: {eta_str}"
                )
            else:
                self.logger.debug(
                    f"Processed {total_rows_processed:,} rows so far - {rows_per_second:.0f} rows/sec"
                )

        if chunk_num % 10 == 0:
            MemoryMonitor.monitor_memory_usage(
                self.logger, f"After processing chunk {chunk_num + 1}"
            )

        # Final stats
        total_time = time.time() - start_time
        progress.finish()

        # Calculate overall processing speed
        avg_rows_per_second = total_rows_processed / total_time if total_time > 0 else 0

        self.logger.info(f"Large dataset processing completed in {total_time:.1f} seconds")
        self.logger.info(f"Processed {total_rows_processed:,} rows in {chunk_count} chunks")
        self.logger.info(f"Average processing speed: {avg_rows_per_second:.0f} rows/second")

        if output_path:
            self.logger.info(f"Processed data saved to {output_path}")

        # Log memory usage after processing
        MemoryMonitor.monitor_memory_usage(self.logger, f"After {description}")

        return total_rows_processed
