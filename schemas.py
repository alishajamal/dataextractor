from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

DocType = Literal["invoice", "contract", "receipt", "other"]

class DocumentClassification(BaseModel):
    detected_type: DocType

class _FieldBase(BaseModel):
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence between 0.0 and 1.0. Must be 0.0 when value is null.",
    )
    source_text: Optional[str] = Field(
        default=None,
        description="Exact verbatim snippet from the document. Null when value is null.",
    )

    @model_validator(mode="after")
    def _enforce_null_rules(self):
        if getattr(self, "value", None) in (None, "", []):
            self.value = None
            self.confidence = 0.0
            self.source_text = None
        return self


class StrField(_FieldBase):
    value: Optional[str] = Field(default=None, description="Extracted text. Null if missing.")


class NumField(_FieldBase):
    value: Optional[float] = Field(
        default=None,
        description="Extracted number, plain (no currency symbol or thousands separator). Null if missing.",
    )


class StrListField(_FieldBase):
    value: Optional[List[str]] = Field(default=None, description="Extracted list of strings. Null if missing.")


class ExtractionBase(BaseModel):
    is_readable: bool = Field(description="False if the document is illegible, corrupted or empty.")
    detected_type: DocType = Field(description="What kind of document this actually is.")
    rejection_reason: Optional[str] = Field(default=None, description="Why it is unreadable, if is_readable is false.")


GATE_FIELDS = {"is_readable", "detected_type", "rejection_reason"}


class InvoiceLineItem(BaseModel):
    description: StrField
    quantity: NumField
    unit_price: NumField
    line_total: NumField


class InvoiceExtraction(ExtractionBase):
    vendor_name: StrField
    invoice_number: StrField
    issue_date: StrField
    line_items: List[InvoiceLineItem] = Field(default_factory=list)
    subtotal: NumField
    tax: NumField
    total: NumField


class ContractExtraction(ExtractionBase):
    parties_involved: StrListField
    start_date: StrField
    end_date: StrField
    payment_terms: StrField
    termination_clause: StrField
    jurisdiction: StrField


class ReceiptItem(BaseModel):
    description: StrField
    price: NumField


class ReceiptExtraction(ExtractionBase):
    merchant: StrField
    purchase_date: StrField
    items_purchased: List[ReceiptItem] = Field(default_factory=list, description="List of items purchased.")
    subtotal: NumField
    tax: NumField
    total: NumField
    payment_method: StrField


class ExtractionMetadata(BaseModel):
    document_type: str
    pipeline_used: str
    file_size_bytes: int
    processing_time_ms: Dict[str, float]


class FinalResponse(BaseModel):
    status: str
    metadata: ExtractionMetadata
    quality_report: Dict[str, list]
    data: Dict[str, Any]