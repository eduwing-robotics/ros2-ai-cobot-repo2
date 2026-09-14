import pytest
from pydantic import ValidationError
from shared.enums.ai import Intent
from shared.schemas.ai import StructuredCommand, TextInterpretRequest
def cmd(**kw):
 d=dict(intent=Intent.CREATE_PRODUCTION_REQUEST,product_name='A형',quantity=1,requires_confirmation=True,clarification_needed=False); d.update(kw); return StructuredCommand(**d)
def test_normal_production(): assert cmd().quantity == 1
def test_quantity_zero_rejected():
 with pytest.raises(ValidationError): cmd(quantity=0)
def test_clarification_message_required():
 with pytest.raises(ValidationError): cmd(product_name=None,requires_confirmation=False,clarification_needed=True,clarification_message=None)
def test_unknown_requires_question():
 with pytest.raises(ValidationError): StructuredCommand(intent=Intent.UNKNOWN)
def test_query_no_confirmation():
 with pytest.raises(ValidationError): StructuredCommand(intent=Intent.QUERY_JOB_STATUS, requires_confirmation=True)
def test_text_strips(): assert TextInterpretRequest(text=' hi ').text == 'hi'
