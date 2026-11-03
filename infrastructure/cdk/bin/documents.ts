#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { buildApp } from '../lib/app';
import { deployConfig } from '../lib/config';

const app = new cdk.App();
buildApp(app, deployConfig(app));
