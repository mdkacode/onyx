from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from typing import TYPE_CHECKING

from pydantic import BaseModel

from onyx.image_gen.exceptions import ImageProviderCredentialsError
from onyx.image_gen.interfaces import ImageGenerationProvider
from onyx.image_gen.interfaces import ImageGenerationProviderCredentials
from onyx.image_gen.interfaces import ReferenceImage
from onyx.tracing.flows import LLMFlow
from onyx.tracing.llm_utils import traced_llm_call

if TYPE_CHECKING:
    from onyx.image_gen.interfaces import ImageGenerationResponse


# LiteLLM routes by prefix: `gemini/*` hits the Google AI Studio (Generative
# Language) API with a plain API key, as opposed to `vertex_ai/*`, which needs a
# GCP service account. Within that, LiteLLM sends models whose name contains
# "gemini" to `:generateContent` and Imagen models to `:predict`, so both
# families work through the same call.
_GEMINI_PREFIX = "gemini/"


class GeminiCredentials(BaseModel):
    api_key: str


class GeminiImageGenerationProvider(ImageGenerationProvider):
    """Image generation via Google AI Studio.

    Distinct from `VertexImageGenerationProvider`: same underlying models, but
    authenticated with an AI Studio API key instead of service-account
    credentials, so it needs no GCP project or location.
    """

    def __init__(self, credentials: GeminiCredentials):
        self._api_key = credentials.api_key

    @classmethod
    def validate_credentials(
        cls,
        credentials: ImageGenerationProviderCredentials,
    ) -> bool:
        try:
            _parse_to_gemini_credentials(credentials)
            return True
        except ImageProviderCredentialsError:
            return False

    @classmethod
    def _build_from_credentials(
        cls,
        credentials: ImageGenerationProviderCredentials,
    ) -> GeminiImageGenerationProvider:
        return cls(credentials=_parse_to_gemini_credentials(credentials))

    @property
    def supports_reference_images(self) -> bool:
        return True

    @property
    def max_reference_images(self) -> int:
        # Gemini image editing supports up to 14 input images.
        return 14

    def generate_image(
        self,
        prompt: str,
        model: str,
        size: str,
        n: int,
        quality: str | None = None,
        reference_images: list[ReferenceImage] | None = None,
        **kwargs: Any,
    ) -> ImageGenerationResponse:
        if reference_images:
            return self._generate_image_with_reference_images(
                prompt=prompt,
                model=model,
                size=size,
                n=n,
                reference_images=reference_images,
            )

        from litellm import image_generation

        with traced_llm_call(
            flow=LLMFlow.IMAGE_GENERATION,
            model=model,
            provider="gemini",
            input_messages=[{"role": "user", "content": prompt}],
        ):
            return image_generation(
                prompt=prompt,
                model=_with_gemini_prefix(model),
                size=size,
                n=n,
                quality=quality,
                api_key=self._api_key,
                **kwargs,
            )

    def _generate_image_with_reference_images(
        self,
        prompt: str,
        model: str,
        size: str,
        n: int,
        reference_images: list[ReferenceImage],
    ) -> ImageGenerationResponse:
        from google import genai
        from google.genai import types as genai_types
        from litellm.types.utils import ImageObject
        from litellm.types.utils import ImageResponse

        client = genai.Client(api_key=self._api_key)

        parts: list[genai_types.Part] = [
            genai_types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
            for image in reference_images
        ]
        parts.append(genai_types.Part.from_text(text=prompt))

        config = genai_types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            candidate_count=max(1, n),
            image_config=genai_types.ImageConfig(
                aspect_ratio=_map_size_to_aspect_ratio(size)
            ),
        )
        model_name = _without_gemini_prefix(model)
        with traced_llm_call(
            flow=LLMFlow.IMAGE_EDIT,
            model=model_name,
            provider="gemini",
            input_messages=[{"role": "user", "content": prompt}],
        ):
            response = client.models.generate_content(
                model=model_name,
                contents=genai_types.Content(
                    role="user",
                    parts=parts,
                ),
                config=config,
            )

        generated_data: list[ImageObject] = []
        for candidate in response.candidates or []:
            candidate_content = candidate.content
            if not candidate_content:
                continue

            for part in candidate_content.parts or []:
                inline_data = part.inline_data
                if not inline_data or inline_data.data is None:
                    continue

                if isinstance(inline_data.data, bytes):
                    b64_json = base64.b64encode(inline_data.data).decode("utf-8")
                elif isinstance(inline_data.data, str):
                    b64_json = inline_data.data
                else:
                    continue

                generated_data.append(
                    ImageObject(
                        b64_json=b64_json,
                        revised_prompt=prompt,
                    )
                )

        if not generated_data:
            raise RuntimeError("No image data returned from Gemini.")

        return ImageResponse(
            created=int(datetime.now().timestamp()),
            data=generated_data,
        )


def _with_gemini_prefix(model: str) -> str:
    """LiteLLM needs the `gemini/` prefix to route to AI Studio."""
    return model if model.startswith(_GEMINI_PREFIX) else f"{_GEMINI_PREFIX}{model}"


def _without_gemini_prefix(model: str) -> str:
    """The google-genai SDK takes the bare model id."""
    return model[len(_GEMINI_PREFIX) :] if model.startswith(_GEMINI_PREFIX) else model


def _map_size_to_aspect_ratio(size: str) -> str:
    return {
        "1024x1024": "1:1",
        "1792x1024": "16:9",
        "1024x1792": "9:16",
        "1536x1024": "3:2",
        "1024x1536": "2:3",
    }.get(size, "1:1")


def _parse_to_gemini_credentials(
    credentials: ImageGenerationProviderCredentials,
) -> GeminiCredentials:
    api_key = credentials.api_key

    # Fall back to custom_config so the key can also be supplied through an
    # OpenAI-compatible LLM provider entry, where the AI Studio key may be
    # stored alongside the Gemini OpenAI-compatibility base URL.
    if not api_key and credentials.custom_config:
        api_key = credentials.custom_config.get("gemini_api_key")

    if not api_key:
        raise ImageProviderCredentialsError("Gemini API key is required")

    return GeminiCredentials(api_key=api_key)
