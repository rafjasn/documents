import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildApp, DocumentsApp } from '../lib/app';
import { deployConfig } from '../lib/config';

/** cdk.json's feature flags, which the CLI would pass. */
const CONTEXT = JSON.parse(readFileSync(join(__dirname, '..', 'cdk.json'), 'utf8')).context;

export interface Synthesized extends DocumentsApp {
    app: cdk.App;
    infraTemplate: Template;
    servicesTemplate: Template;
}

/** Both stacks as CI synthesizes them: placeholder config, no AWS credentials. */
export function synthesize(): Synthesized {
    const app = new cdk.App({ context: CONTEXT });
    const stacks = buildApp(app, deployConfig(app, {}));
    return {
        app,
        ...stacks,
        infraTemplate: Template.fromStack(stacks.infra),
        servicesTemplate: Template.fromStack(stacks.services)
    };
}

type Resource = { Type: string; Properties: Record<string, any> };

export function resources(template: Template, type: string): Record<string, Resource> {
    return template.findResources(type) as Record<string, Resource>;
}

/** The task definition of `documents-<service>`. */
export function taskDefinition(template: Template, service: string): [string, Resource] {
    const found = Object.entries(resources(template, 'AWS::ECS::TaskDefinition')).filter(
        ([, task]) => task.Properties.Family === `documents-${service}`
    );
    if (found.length !== 1) {
        throw new Error(`expected one task definition for ${service}, found ${found.length}`);
    }
    return found[0];
}

/** The logical ID a `Ref` or `Fn::GetAtt` points at. */
export function target(value: any): string | undefined {
    if (value?.Ref) {
        return value.Ref;
    }
    if (value?.['Fn::GetAtt']) {
        return value['Fn::GetAtt'][0];
    }
    return undefined;
}

/** Every statement of the policies attached to a role, one action and resource at a time. */
export function grants(template: Template, roleId: string): [string, string][] {
    const pairs: [string, string][] = [];
    for (const policy of Object.values(resources(template, 'AWS::IAM::Policy'))) {
        if (!policy.Properties.Roles.some((role: any) => target(role) === roleId)) {
            continue;
        }
        for (const statement of policy.Properties.PolicyDocument.Statement) {
            const actions = [statement.Action].flat();
            const list = [statement.Resource].flat();
            for (const action of actions) {
                for (const resource of list) {
                    pairs.push([action, describe(resource)]);
                }
            }
        }
    }
    return pairs;
}

/** A resource as a short string: `*`, an object prefix such as `bucket/uploads/*`, or the
 * logical ID of the resource (or the infra export) it refers to. */
export function describe(resource: any): string {
    if (typeof resource === 'string') {
        return resource;
    }
    const json = JSON.stringify(resource);
    const prefix = /"\/([a-z]+\/\*)"/.exec(json);
    const name = /Exports(?:Output)?(?:Ref|FnGetAtt)([A-Za-z]+?)[0-9A-F]{8}/.exec(json);
    const base = name?.[1] ?? target(resource) ?? json;
    return prefix ? `${base}/${prefix[1]}` : base;
}
