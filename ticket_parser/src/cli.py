"""Command-line interface for ticket parser."""

import argparse
import json
import sys
from pathlib import Path
from ticket_parser import TicketParser, TicketParseError
from jsonschema import validate, ValidationError
from schema import TICKET_SCHEMA


def main():
    parser = argparse.ArgumentParser(
        description='Parse markdown ticket files to JSON'
    )
    parser.add_argument(
        'file',
        type=str,
        help='Path to markdown ticket file'
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        help='Output JSON file (default: stdout)'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate output against JSON schema'
    )

    args = parser.parse_args()

    # Check if file exists
    filepath = Path(args.file)
    if not filepath.exists():
        print(f"Error: File '{args.file}' not found.", file=sys.stderr)
        sys.exit(1)

    # Parse the ticket
    ticket_parser = TicketParser()
    try:
        ticket = ticket_parser.parse_file(str(filepath))
    except TicketParseError as e:
        print(f"Parse error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate if requested
    if args.validate:
        try:
            validate(instance=ticket, schema=TICKET_SCHEMA)
        except ValidationError as e:
            print(f"Validation error: {e.message}", file=sys.stderr)
            sys.exit(1)

    # Output the result
    json_output = json.dumps(ticket, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json_output)
        print(f"Output written to {args.output}")
    else:
        print(json_output)


if __name__ == '__main__':
    main()
