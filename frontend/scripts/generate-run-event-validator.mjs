import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import Ajv2020 from "ajv/dist/2020.js";
import standaloneCode from "ajv/dist/standalone/index.js";
import addFormats from "ajv-formats";
import { build } from "esbuild";

const openApiPath = fileURLToPath(
  new URL("../src/api/openapi.json", import.meta.url)
);
const generatedModulePath = fileURLToPath(
  new URL("../src/api/generated/validateStoredRunEvent.mjs", import.meta.url)
);
const generatedDeclarationPath = fileURLToPath(
  new URL("../src/api/generated/validateStoredRunEvent.d.mts", import.meta.url)
);
const generatedBanner = "// Generated from src/api/openapi.json. Do not edit.\n";
const declaration = `${generatedBanner}import type { StoredRunEvent } from "../contracts";

export default function validateStoredRunEvent(
  value: unknown
): value is StoredRunEvent;
`;

const openApi = JSON.parse(await readFile(openApiPath, "utf8"));
const componentSchemas = openApi.components?.schemas;
if (!componentSchemas?.StoredRunEvent) {
  throw new Error("OpenAPI component StoredRunEvent is missing");
}

const referencedComponentNames = new Set(["StoredRunEvent"]);
const pendingComponentNames = ["StoredRunEvent"];
while (pendingComponentNames.length > 0) {
  const componentName = pendingComponentNames.pop();
  const componentSchema = componentSchemas[componentName];
  if (!componentSchema) {
    throw new Error(`OpenAPI component ${componentName} is missing`);
  }
  const pendingValues = [componentSchema];
  while (pendingValues.length > 0) {
    const value = pendingValues.pop();
    if (!value || typeof value !== "object") continue;
    if (!Array.isArray(value) && typeof value.$ref === "string") {
      const match = /^#\/components\/schemas\/([^/]+)$/.exec(value.$ref);
      if (match && !referencedComponentNames.has(match[1])) {
        referencedComponentNames.add(match[1]);
        pendingComponentNames.push(match[1]);
      }
    }
    pendingValues.push(
      ...(Array.isArray(value) ? value : Object.values(value))
    );
  }
}

const validationSchema = {
  $id: "urn:testing-react-agent:stored-run-event",
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $ref: "#/components/schemas/StoredRunEvent",
  components: {
    schemas: Object.fromEntries(
      [...referencedComponentNames]
        .sort()
        .map((componentName) => [componentName, componentSchemas[componentName]])
    )
  }
};
const ajv = new Ajv2020({
  strict: false,
  code: { source: true, esm: true }
});
addFormats(ajv, {
  formats: ["date-time"],
  keywords: false
});
const validateStoredRunEvent = ajv.compile(validationSchema);
const standaloneEntry = standaloneCode(ajv, validateStoredRunEvent);
const bundle = await build({
  bundle: true,
  format: "esm",
  minify: true,
  platform: "browser",
  stdin: {
    contents: standaloneEntry,
    loader: "js",
    resolveDir: fileURLToPath(new URL("..", import.meta.url)),
    sourcefile: "validateStoredRunEvent.js"
  },
  write: false
});
const generatedModule = `${generatedBanner}${bundle.outputFiles[0].text}`;

async function assertCurrent(path, expected) {
  let current;
  try {
    current = await readFile(path, "utf8");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  if (current !== expected) {
    process.stderr.write(
      "Generated SSE validator is stale. Run `npm run generate:api`.\n"
    );
    process.exitCode = 1;
  }
}

if (process.argv.includes("--check")) {
  await Promise.all([
    assertCurrent(generatedModulePath, generatedModule),
    assertCurrent(generatedDeclarationPath, declaration)
  ]);
} else {
  await mkdir(dirname(generatedModulePath), { recursive: true });
  await Promise.all([
    writeFile(generatedModulePath, generatedModule, "utf8"),
    writeFile(generatedDeclarationPath, declaration, "utf8")
  ]);
}
