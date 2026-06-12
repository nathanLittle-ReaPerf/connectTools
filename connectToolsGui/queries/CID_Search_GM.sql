fields @timestamp, @message, @logStream, @log, ContactId, Parameters.Key as Attribute,
Parameters.Value as Value, Results, Parameters.SecondValue as Check, Parameters.Parameters.sf_operation as Operation, Parameters.FunctionArn
| filter ContactId = '{ CID }'
| parse @message '*"Text":"*","*' as start, Prompt, end
| parse @message 'ExternalResults":*,"Parameters":*,"T' as External_Results, Parameters
| parse Parameters.FunctionArn '*on:*' as instance, Function
| display @timestamp, ContactId, ContactFlowName, ContactFlowModuleType, Attribute, Value, Check, Results, Prompt, Operation, Function, Parameters, External_Results
| sort @timestamp asc
