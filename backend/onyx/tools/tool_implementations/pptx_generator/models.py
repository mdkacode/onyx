from pydantic import BaseModel


class FinalPptxGenerationResponse(BaseModel):
    file_id: str
    file_url: str
    title: str
    num_slides: int
