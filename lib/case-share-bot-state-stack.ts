import { RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import { AttributeType, BillingMode, Table } from "aws-cdk-lib/aws-dynamodb";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

export class CaseShareBotStateStack extends Stack {
  public readonly casesTable: Table;
  public readonly threadStateTable: Table;
  public readonly eventDedupeTable: Table;
  public readonly shareLogTable: Table;
  public readonly caseAssetsBucket: Bucket;

  public constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    this.casesTable = new Table(this, "CasesTable", {
      partitionKey: { name: "case_id", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN
    });

    this.threadStateTable = new Table(this, "ThreadStateTable", {
      partitionKey: { name: "thread_key", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expires_at",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN
    });

    this.eventDedupeTable = new Table(this, "EventDedupeTable", {
      partitionKey: { name: "event_id", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expires_at",
      removalPolicy: RemovalPolicy.RETAIN
    });

    this.shareLogTable = new Table(this, "ShareLogTable", {
      partitionKey: { name: "case_id", type: AttributeType.STRING },
      sortKey: { name: "shared_at", type: AttributeType.STRING },
      billingMode: BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.RETAIN
    });

    this.caseAssetsBucket = new Bucket(this, "CaseAssetsBucket", {
      encryption: BucketEncryption.S3_MANAGED,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN
    });
  }
}
