import json,os,sys,urllib.request
tok=json.load(open(os.path.expanduser('~/.railway/config.json')))['user']['token']
ENV='fdef304f-a274-43c6-b59c-0a99f8588e0d'
def gql(q,v):
    r=urllib.request.Request('https://backboard.railway.com/graphql/v2',data=json.dumps({'query':q,'variables':v}).encode(),headers={'Authorization':'Bearer '+tok,'Content-Type':'application/json','User-Agent':'curl/8'})
    return json.load(urllib.request.urlopen(r))
q='''query($s:String!,$e:String!){deployments(first:4,input:{serviceId:$s,environmentId:$e}){edges{node{id status createdAt staticUrl meta}}}}'''
for name,sid in [('backend','be3ad2bc-7303-4672-ae17-649c62c86a87'),('mcp','30ce98e2-80a2-4cd5-8fb8-42c23247620f')]:
    d=gql(q,{'s':sid,'e':ENV})
    if 'errors' in d: print(name,d['errors']); continue
    for e in d['data']['deployments']['edges']:
        n=e['node']; m=n.get('meta') or {}
        print(name,n['id'],n['status'],n['createdAt'],m.get('commitHash'),(m.get('commitMessage') or '')[:60].replace('\n',' '),m.get('repo'),m.get('branch'))
