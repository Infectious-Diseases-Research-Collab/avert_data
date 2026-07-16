#!/usr/bin/env python3
"""Run the full AVERT data pipeline: download new zips, then merge into csvs."""

import sys

import download_data
import process_data


def main():
    download_rc = download_data.main()
    process_rc = process_data.main()
    return download_rc or process_rc


if __name__ == "__main__":
    sys.exit(main())
