export class ApiError extends Error { constructor(message:string, public status:number){super(message)} }
const pendingGets=new Map<string,Promise<unknown>>()
export async function api<T>(path:string, options:RequestInit={}):Promise<T>{
  const method=(options.method||'GET').toUpperCase()
  if(method==='GET'&&pendingGets.has(path))return pendingGets.get(path) as Promise<T>
  const run=(async()=>{const response=await fetch(path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});let data:any=null;try{data=await response.json()}catch{}if(!response.ok)throw new ApiError(data?.detail||`请求失败 (${response.status})`,response.status);return data as T})()
  if(method==='GET')pendingGets.set(path,run)
  try{return await run}finally{if(method==='GET')pendingGets.delete(path)}
}
