"""Tests for ticket parser functionality."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from ticket_parser import TicketParser, TicketParseError
from schema import TICKET_SCHEMA
from jsonschema import validate, ValidationError


@pytest.fixture
def parser():
    return TicketParser()


@pytest.fixture
def sample_files_dir():
    return Path(__file__).parent.parent / 'samples'


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / 'fixtures'


class TestTicketParser:
    """Tests for basic parsing functionality."""

    def test_parse_valid_ticket(self, parser, fixtures_dir):
        """Test parsing a valid ticket markdown."""
        result = parser.parse_file(str(fixtures_dir / 'valid_ticket.md'))

        assert result['name'] == 'Fix Bug in Authentication'
        assert result['description'] == 'Users are experiencing login issues.'
        assert len(result['tasks']) == 3
        assert result['tasks'][0] == 'Investigate the problem.'
        assert result['tasks'][1] == 'Fix the bug.'
        assert result['tasks'][2] == 'Deploy the fix.'

    def test_parse_multiline_description(self, parser, fixtures_dir):
        """Test parsing ticket with multi-paragraph description."""
        result = parser.parse_file(str(fixtures_dir / 'multiline_description.md'))

        assert result['name'] == 'Test Ticket'
        assert 'first paragraph' in result['description']
        assert 'second paragraph' in result['description']
        assert len(result['tasks']) == 2

    def test_missing_ticket_name(self, parser, fixtures_dir):
        """Test that missing H1 raises an error."""
        with pytest.raises(TicketParseError, match="No ticket name"):
            parser.parse_file(str(fixtures_dir / 'missing_ticket_name.md'))

    def test_missing_description(self, parser, fixtures_dir):
        """Test that missing description raises an error."""
        with pytest.raises(TicketParseError, match="No description"):
            parser.parse_file(str(fixtures_dir / 'missing_description.md'))

    def test_missing_tasks(self, parser, fixtures_dir):
        """Test that missing tasks raises an error."""
        with pytest.raises(TicketParseError, match="No tasks"):
            parser.parse_file(str(fixtures_dir / 'missing_tasks.md'))

    def test_invalid_section_header(self, parser, fixtures_dir):
        """Test that invalid section headers raise an error."""
        with pytest.raises(TicketParseError, match="Invalid section header"):
            parser.parse_file(str(fixtures_dir / 'invalid_section_header.md'))

    def test_multiple_h1_headings(self, parser, fixtures_dir):
        """Test that multiple H1 headings raise an error."""
        with pytest.raises(TicketParseError, match="Multiple H1 headings"):
            parser.parse_file(str(fixtures_dir / 'multiple_h1_headings.md'))

    def test_bullet_points_in_description(self, parser, fixtures_dir):
        """Test that bullet points in description raise an error."""
        with pytest.raises(TicketParseError, match="Bullet points found outside"):
            parser.parse_file(str(fixtures_dir / 'bullet_points_in_description.md'))

    def test_paragraph_in_tasks(self, parser, fixtures_dir):
        """Test that paragraphs in tasks section raise an error."""
        with pytest.raises(TicketParseError, match="Only bullet points allowed"):
            parser.parse_file(str(fixtures_dir / 'paragraph_in_tasks.md'))


class TestSampleFiles:
    """Tests for parsing sample files."""

    def test_parse_ticket1(self, parser, sample_files_dir):
        """Test parsing ticket1.md sample file."""
        result = parser.parse_file(str(sample_files_dir / 'ticket1.md'))

        assert result['name'] == 'Fix Bug in User Authentication'
        assert 'intermittent login failures' in result['description']
        assert len(result['tasks']) == 5

    def test_parse_ticket2(self, parser, sample_files_dir):
        """Test parsing ticket2.md sample file."""
        result = parser.parse_file(str(sample_files_dir / 'ticket2.md'))

        assert result['name'] == 'Implement Dark Mode Feature'
        assert 'dark mode option' in result['description']
        assert len(result['tasks']) == 6

    def test_parse_ticket3(self, parser, sample_files_dir):
        """Test parsing ticket3.md sample file."""
        result = parser.parse_file(str(sample_files_dir / 'ticket3.md'))

        assert result['name'] == 'Optimize Database Query Performance'
        assert 'slow load times' in result['description']
        assert len(result['tasks']) == 4


class TestJSONSchemaValidation:
    """Tests for JSON schema validation."""

    def test_valid_ticket_passes_schema(self, parser, fixtures_dir):
        """Test that a valid ticket passes schema validation."""
        result = parser.parse_file(str(fixtures_dir / 'valid_ticket_for_schema.md'))

        # Should not raise an exception
        validate(instance=result, schema=TICKET_SCHEMA)

    def test_missing_name_fails_schema(self):
        """Test that missing name fails schema validation."""
        invalid_ticket = {
            'description': 'A description',
            'tasks': ['Task 1']
        }

        with pytest.raises(ValidationError):
            validate(instance=invalid_ticket, schema=TICKET_SCHEMA)

    def test_missing_description_fails_schema(self):
        """Test that missing description fails schema validation."""
        invalid_ticket = {
            'name': 'Ticket Name',
            'tasks': ['Task 1']
        }

        with pytest.raises(ValidationError):
            validate(instance=invalid_ticket, schema=TICKET_SCHEMA)

    def test_missing_tasks_fails_schema(self):
        """Test that missing tasks fails schema validation."""
        invalid_ticket = {
            'name': 'Ticket Name',
            'description': 'A description'
        }

        with pytest.raises(ValidationError):
            validate(instance=invalid_ticket, schema=TICKET_SCHEMA)

    def test_empty_tasks_array_fails_schema(self):
        """Test that empty tasks array fails schema validation."""
        invalid_ticket = {
            'name': 'Ticket Name',
            'description': 'A description',
            'tasks': []
        }

        with pytest.raises(ValidationError):
            validate(instance=invalid_ticket, schema=TICKET_SCHEMA)

    def test_additional_properties_fail_schema(self):
        """Test that additional properties fail schema validation."""
        invalid_ticket = {
            'name': 'Ticket Name',
            'description': 'A description',
            'tasks': ['Task 1'],
            'extra_field': 'Not allowed'
        }

        with pytest.raises(ValidationError):
            validate(instance=invalid_ticket, schema=TICKET_SCHEMA)

    def test_all_sample_files_pass_schema(self, parser, sample_files_dir):
        """Test that all sample files produce valid JSON according to schema."""
        for sample_file in sample_files_dir.glob('*.md'):
            result = parser.parse_file(str(sample_file))
            # Should not raise an exception
            validate(instance=result, schema=TICKET_SCHEMA)

    def test_invalid_ticket_file(self, parser, fixtures_dir):
        """Test that invalid ticket file fails validation."""
        try:
            parser.parse_file(str(fixtures_dir / 'invalid_ticket.md'))
            pytest.fail("Expected TicketParseError for missing Tasks section")
        except TicketParseError:
            pass
