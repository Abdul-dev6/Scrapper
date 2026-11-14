import { z } from "zod";
import { CATEGORY_IDS, SOURCES } from "./categories";
import { PLACE_ID_RE } from "./placeText";

export const loginSchema = z.object({
  email: z.string().trim().toLowerCase().email().max(254),
  password: z.string().min(1).max(200),
});

export const signupSchema = z.object({
  email: z.string().trim().toLowerCase().email("Enter a valid email address").max(254),
  password: z
    .string()
    .min(12, "Password must be at least 12 characters")
    // bcrypt only uses the first 72 bytes; refuse longer so two different passwords can't match
    .refine((p) => new TextEncoder().encode(p).length <= 72, "Password must be at most 72 characters"),
});

export const createJobSchema = z.object({
  location: z
    .string()
    .trim()
    .min(2, "Enter a city, region or country")
    .max(120)
    // letters (any language), digits, spaces and common address punctuation only
    .regex(/^[\p{L}\p{N}\s,.'()\-]+$/u, "Location contains unsupported characters"),
  // The place the user picked from the search results (gazetteer id). Required: no free-text geocoding.
  placeId: z.string().regex(PLACE_ID_RE, "Pick a place from the list"),
  categories: z.array(z.enum(CATEGORY_IDS)).min(1, "Pick at least one category").max(CATEGORY_IDS.length),
  sources: z.array(z.enum(SOURCES)).min(1).max(SOURCES.length).default(["osm"]),
});

export type CreateJobInput = z.infer<typeof createJobSchema>;

/** A MongoDB ObjectId in a URL, e.g. /api/jobs/6af3.../cancel */
export const objectIdSchema = z.string().regex(/^[a-f\d]{24}$/i, "Invalid id");

/** Fields the user may change on a lead. Everything else is written only by the worker. */
export const leadPatchSchema = z
  .object({
    saved: z.boolean().optional(),
    status: z.enum(["new", "contacted", "not_interested"]).optional(),
    notes: z.string().max(5000).optional(),
    // A person confirms (or un-confirms) one email/phone of this lead, identified by its value.
    verifyContact: z
      .object({
        kind: z.enum(["email", "phone"]),
        value: z.string().trim().min(3).max(254),
        verified: z.boolean(),
      })
      .strict()
      .optional(),
  })
  .strict() // reject unknown fields, e.g. someone trying to set ownerId or emails
  .refine((v) => Object.keys(v).length > 0, "Nothing to update");

export const suppressionAddSchema = z.object({
  type: z.enum(["email", "phone", "domain"]),
  value: z.string().trim().min(3, "Enter a value").max(254),
  reason: z.string().trim().max(500).optional(),
});

export const passwordChangeSchema = z.object({
  current: z.string().min(1).max(200),
  next: signupSchema.shape.password,
});
