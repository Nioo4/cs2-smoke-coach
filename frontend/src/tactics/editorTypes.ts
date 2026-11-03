export interface Point { x: number; y: number }
interface ActionBase { id: string; group_id: string; version: number }
export type TacticAction = ActionBase & (
  | { kind: 'move'; role_id: string; path: Point[] }
  | { kind: 'wait'; role_id: string }
  | { kind: 'observe'; role_id: string; target: Point }
  | { kind: 'utility'; thrower_role_id: string; utility_type: 'smoke' | 'flash' | 'molotov' | 'he' | 'decoy'; target: Point }
  | { kind: 'text'; role_id: string | null; position: Point }
);
export interface TacticStage { id: string; version: number; initial_positions: Record<string, Point & { version: number }>; actions: TacticAction[]; lineup_ids: string[] }
export interface TacticStructure { schema_kind: 'stage-board'; schema_version: 2; document_id: string; map: 'mirage'; side: 'T' | 'CT'; roles: { id: string; number: number; color: string }[]; stages: TacticStage[] }
export interface TacticProjection { structure: TacticStructure; fields: Record<string, string> }
export interface PlaybackFrame { positions: Record<string, Point>; activeGroupId: string | null; activeActionIds: string[]; complete: boolean; progress: number }
