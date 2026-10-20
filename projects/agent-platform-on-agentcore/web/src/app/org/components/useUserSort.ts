import { useState } from "react";
import { firstSortDirection } from "../orgTable.mjs";

export type UserSortKey = "email" | "role" | "teams" | "status" | "last_login" | "turns_30d" | "model_cost_micros_30d";
export type UserSort = { key: UserSortKey; dir: "asc" | "desc" };

/** The people tables' sort: a new column starts in its natural direction, the same column flips. */
export function useUserSort(initial: UserSort = { key: "email", dir: "asc" }) {
  const [sort, setSort] = useState<UserSort>(initial);
  const toggle = (key: UserSortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: firstSortDirection(key) }));
  return { sort, toggle };
}
