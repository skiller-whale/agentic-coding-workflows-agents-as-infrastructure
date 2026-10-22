"""Markdown ticket parser using markdown_it."""

from markdown_it import MarkdownIt
from typing import Dict, List, Any


class TicketParseError(Exception):
    """Exception raised for ticket parsing errors."""
    pass


class TicketParser:
    """Parser for markdown ticket files."""

    def __init__(self):
        self.md = MarkdownIt()

    def parse(self, markdown_content: str) -> Dict[str, Any]:
        """
        Parse markdown content into a ticket dictionary.

        Args:
            markdown_content: The markdown string to parse

        Returns:
            Dictionary with 'name', 'description', and 'tasks' keys

        Raises:
            TicketParseError: If the markdown format is invalid
        """
        tokens = self.md.parse(markdown_content)

        ticket = {
            'name': '',
            'description': '',
            'tasks': []
        }

        current_section = None
        current_text = []

        i = 0
        while i < len(tokens):
            token = tokens[i]

            # H1 heading - should be the ticket name
            if token.type == 'heading_open' and token.tag == 'h1':
                if ticket['name']:
                    raise TicketParseError("Multiple H1 headings found. Only one ticket name allowed.")
                # Next token should be inline with the text
                i += 1
                if i < len(tokens) and tokens[i].type == 'inline':
                    ticket['name'] = tokens[i].content.strip()
                current_section = None
                i += 1  # Skip the heading_close

            # H2 heading - section headers (Description or Tasks)
            elif token.type == 'heading_open' and token.tag == 'h2':
                # Save any accumulated text from previous section
                if current_section == 'description' and current_text:
                    ticket['description'] = ' '.join(current_text).strip()
                    current_text = []

                i += 1
                if i < len(tokens) and tokens[i].type == 'inline':
                    section_name = tokens[i].content.strip()
                    if section_name == 'Description':
                        current_section = 'description'
                    elif section_name == 'Tasks':
                        current_section = 'tasks'
                    else:
                        raise TicketParseError(f"Invalid section header: {section_name}. Only 'Description' and 'Tasks' allowed.")
                i += 1  # Skip the heading_close

            # Paragraph - should only be in Description
            elif token.type == 'paragraph_open':
                if current_section != 'description':
                    if current_section == 'tasks':
                        raise TicketParseError("Only bullet points allowed under 'Tasks' section.")
                    elif current_section is None:
                        raise TicketParseError("Content found outside of Description or Tasks sections.")
                i += 1
                if i < len(tokens) and tokens[i].type == 'inline':
                    current_text.append(tokens[i].content.strip())
                i += 1  # Skip paragraph_close

            # Bullet list - should only be in Tasks
            elif token.type == 'bullet_list_open':
                if current_section != 'tasks':
                    raise TicketParseError("Bullet points found outside of 'Tasks' section.")
                i += 1
                # Process list items
                while i < len(tokens) and tokens[i].type != 'bullet_list_close':
                    if tokens[i].type == 'list_item_open':
                        i += 1
                        # Skip paragraph_open if present
                        if i < len(tokens) and tokens[i].type == 'paragraph_open':
                            i += 1
                        # Get the inline content
                        if i < len(tokens) and tokens[i].type == 'inline':
                            task_text = tokens[i].content.strip()
                            if task_text:
                                ticket['tasks'].append(task_text)
                            i += 1
                        # Skip paragraph_close if present
                        if i < len(tokens) and tokens[i].type == 'paragraph_close':
                            i += 1
                        # Skip list_item_close
                        if i < len(tokens) and tokens[i].type == 'list_item_close':
                            i += 1
                    else:
                        i += 1

            else:
                i += 1

        # Save any remaining description text
        if current_section == 'description' and current_text:
            ticket['description'] = ' '.join(current_text).strip()

        # Validate required fields
        if not ticket['name']:
            raise TicketParseError("No ticket name (H1 heading) found.")
        if not ticket['description']:
            raise TicketParseError("No description found.")
        if not ticket['tasks']:
            raise TicketParseError("No tasks found.")

        return ticket

    def parse_file(self, filepath: str) -> Dict[str, Any]:
        """
        Parse a markdown file into a ticket dictionary.

        Args:
            filepath: Path to the markdown file

        Returns:
            Dictionary with 'name', 'description', and 'tasks' keys
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        return self.parse(content)
