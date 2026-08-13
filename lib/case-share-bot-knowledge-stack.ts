import { CfnOutput, CfnResource, RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import * as bedrock from "aws-cdk-lib/aws-bedrock";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import { Bucket } from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

export interface CaseShareBotKnowledgeStackProps extends StackProps {
  readonly caseAssetsBucket: Bucket;
}

export const CASE_KNOWLEDGE_SOURCE_PREFIX = "knowledge-base/cases/";

const EMBEDDING_MODEL_ID = "cohere.embed-multilingual-v3";
const EMBEDDING_DIMENSION = 1024;

export class CaseShareBotKnowledgeStack extends Stack {
  public readonly knowledgeBaseId: string;
  public readonly knowledgeBaseArn: string;
  public readonly dataSourceId: string;

  public constructor(scope: Construct, id: string, props: CaseShareBotKnowledgeStackProps) {
    super(scope, id, props);

    const projectTag = this.node.tryGetContext("projectTag") as string | undefined;
    const vectorTags = [{ Key: "Project", Value: projectTag ?? "jirei-share-bot" }];

    const vectorBucket = new CfnResource(this, "CaseVectorBucket", {
      type: "AWS::S3Vectors::VectorBucket",
      properties: {
        Tags: vectorTags
      }
    });
    vectorBucket.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const vectorIndex = new CfnResource(this, "CaseVectorIndex", {
      type: "AWS::S3Vectors::Index",
      properties: {
        VectorBucketArn: vectorBucket.getAtt("VectorBucketArn").toString(),
        DataType: "float32",
        Dimension: EMBEDDING_DIMENSION,
        DistanceMetric: "cosine",
        MetadataConfiguration: {
          NonFilterableMetadataKeys: [
            "AMAZON_BEDROCK_TEXT",
            "AMAZON_BEDROCK_METADATA"
          ]
        },
        Tags: vectorTags
      }
    });
    vectorIndex.addResourceDependency(vectorBucket);
    vectorIndex.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const knowledgeBaseRole = new Role(this, "KnowledgeBaseRole", {
      assumedBy: new ServicePrincipal("bedrock.amazonaws.com", {
        conditions: {
          StringEquals: {
            "aws:SourceAccount": this.account
          },
          ArnLike: {
            "aws:SourceArn": `arn:aws:bedrock:${this.region}:${this.account}:knowledge-base/*`
          }
        }
      })
    });

    knowledgeBaseRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["bedrock:InvokeModel"],
        resources: [
          Stack.of(this).formatArn({
            service: "bedrock",
            resource: "foundation-model",
            resourceName: EMBEDDING_MODEL_ID,
            account: ""
          })
        ]
      })
    );
    knowledgeBaseRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["s3:ListBucket"],
        resources: [props.caseAssetsBucket.bucketArn],
        conditions: {
          StringLike: {
            "s3:prefix": [`${CASE_KNOWLEDGE_SOURCE_PREFIX}*`]
          }
        }
      })
    );
    knowledgeBaseRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [props.caseAssetsBucket.arnForObjects(`${CASE_KNOWLEDGE_SOURCE_PREFIX}*`)]
      })
    );
    knowledgeBaseRole.addToPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: [
          "s3vectors:GetIndex",
          "s3vectors:PutVectors",
          "s3vectors:GetVectors",
          "s3vectors:DeleteVectors",
          "s3vectors:QueryVectors"
        ],
        resources: [vectorIndex.getAtt("IndexArn").toString()]
      })
    );

    const knowledgeBase = new bedrock.CfnKnowledgeBase(this, "CaseKnowledgeBase", {
      name: "jirei_share_bot_kb",
      description: "Internal case-sharing semantic search knowledge base.",
      roleArn: knowledgeBaseRole.roleArn,
      knowledgeBaseConfiguration: {
        type: "VECTOR",
        vectorKnowledgeBaseConfiguration: {
          embeddingModelArn: Stack.of(this).formatArn({
            service: "bedrock",
            resource: "foundation-model",
            resourceName: EMBEDDING_MODEL_ID,
            account: ""
          })
        }
      },
      storageConfiguration: {
        type: "S3_VECTORS",
        s3VectorsConfiguration: {
          indexArn: vectorIndex.getAtt("IndexArn").toString()
        }
      }
    });
    knowledgeBase.addResourceDependency(vectorIndex);
    knowledgeBase.node.addDependency(knowledgeBaseRole);
    knowledgeBase.applyRemovalPolicy(RemovalPolicy.RETAIN);

    const dataSource = new bedrock.CfnDataSource(this, "CaseMarkdownDataSource", {
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      name: "jirei_share_bot_cases",
      description: "Sanitized Markdown case records generated from DynamoDB.",
      dataDeletionPolicy: "DELETE",
      dataSourceConfiguration: {
        type: "S3",
        s3Configuration: {
          bucketArn: props.caseAssetsBucket.bucketArn,
          inclusionPrefixes: [CASE_KNOWLEDGE_SOURCE_PREFIX]
        }
      },
      vectorIngestionConfiguration: {
        chunkingConfiguration: {
          chunkingStrategy: "FIXED_SIZE",
          fixedSizeChunkingConfiguration: {
            maxTokens: 300,
            overlapPercentage: 15
          }
        }
      }
    });
    dataSource.addResourceDependency(knowledgeBase);
    dataSource.applyRemovalPolicy(RemovalPolicy.RETAIN);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.knowledgeBaseArn = knowledgeBase.attrKnowledgeBaseArn;
    this.dataSourceId = dataSource.attrDataSourceId;

    new CfnOutput(this, "KnowledgeBaseId", {
      value: this.knowledgeBaseId
    });
    new CfnOutput(this, "KnowledgeBaseArn", {
      value: this.knowledgeBaseArn
    });
    new CfnOutput(this, "KnowledgeBaseDataSourceId", {
      value: this.dataSourceId
    });
    new CfnOutput(this, "KnowledgeBaseSourcePrefix", {
      value: CASE_KNOWLEDGE_SOURCE_PREFIX
    });
  }
}
