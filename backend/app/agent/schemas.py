from typing import Literal

from pydantic import BaseModel, Field


class Spec(BaseModel):
    premise: str = Field(min_length=1, max_length=6000)
    genre: str
    style: str
    ending: str
    chapter_count: int = Field(ge=1, le=30)
    target_words: int = Field(ge=200, le=5000)


class OutlineItem(BaseModel):
    chapter_number: int = Field(ge=1, le=30)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2500)
    beats: list[str] = Field(min_length=1, max_length=12)


class StoryOutline(BaseModel):
    chapters: list[OutlineItem] = Field(min_length=1, max_length=30)


class CharacterData(BaseModel):
    name: str
    role: str
    description: str
    arc: dict


class Bible(BaseModel):
    premise: str
    genre: str
    style: str
    world: dict
    facts: list[str] = Field(max_length=80)
    characters: list[CharacterData] = Field(min_length=1, max_length=30)


class Draft(BaseModel):
    content: str = Field(min_length=20, max_length=100000)


class Issue(BaseModel):
    chapter_number: int = Field(ge=1, le=30)
    severity: Literal["minor", "major"]
    description: str = Field(min_length=1, max_length=2000)


class Review(BaseModel):
    verdict: Literal["pass", "revise"]
    issues: list[Issue] = Field(max_length=40)
    notes: str = Field(max_length=4000)


class Memory(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    facts: list[str] = Field(max_length=30)
    character_states: dict
    open_threads: list[str] = Field(max_length=30)


SCHEMAS = {
    "analyze": Spec,
    "outline": StoryOutline,
    "bible": Bible,
    "write": Draft,
    "revise": Draft,
    "review": Review,
    "final_review": Review,
    "memory": Memory,
}
