/** Prints the monthly cost estimate (lib/cost.ts) as README.md's table: npm run cost. */
import * as cdk from 'aws-cdk-lib';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildApp } from '../lib/app';
import { deployConfig } from '../lib/config';
import { estimate, markdown } from '../lib/cost';

const context = JSON.parse(readFileSync(join(__dirname, '..', 'cdk.json'), 'utf8')).context;
const app = new cdk.App({ context });
const { infra, services } = buildApp(app, deployConfig(app, {}));
const assembly = app.synth();
const templates = [infra, services].map(
    (stack) => assembly.getStackByName(stack.stackName).template
);
console.log(markdown(estimate(templates)));
