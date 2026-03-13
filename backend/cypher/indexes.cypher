CREATE INDEX space_building_floor_idx IF NOT EXISTS FOR (s:Space) ON (s.building_id, s.floor_index);
CREATE INDEX space_type_idx IF NOT EXISTS FOR (s:Space) ON (s.space_type);
CREATE INDEX space_campus_idx IF NOT EXISTS FOR (s:Space) ON (s.campus_id);
CREATE FULLTEXT INDEX space_search_idx IF NOT EXISTS FOR (s:Space) ON EACH [s.display_name, s.short_name, s.tags_text];

// Index for semanti RAG search
CREATE VECTOR INDEX space_embedding_idx IF NOT EXISTS
FOR (s:Space) ON (s.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: 384,
    `vector.similarity_function`: 'cosine'
  }
};