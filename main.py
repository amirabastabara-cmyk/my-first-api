# main.py
# VoidVision Server v28
# FastAPI lightweight edition

import os
import json
import uuid
import bcrypt
import jwt

from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(
    title="VoidVision Server",
    version="28.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# ================= CONFIG =================

JWT_SECRET = os.getenv(
    "JWT_SECRET",
    "voidvision-secret"
)

JWT_ALGORITHM = "HS256"

TOKEN_EXPIRE = 60 * 24 * 7


# ================= DATABASE =================

users = {}

online_users = {}

rooms = {}

friends = {}

requests = {}



# ================= HELPERS =================


def make_token(username, user_id):

    payload = {

        "sub": username,

        "uid": user_id,

        "exp":
        datetime.utcnow()
        +
        timedelta(minutes=TOKEN_EXPIRE)

    }


    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )



def check_token(token):

    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=[JWT_ALGORITHM]
    )



# ================= BASIC TEST =================


@app.get("/")
async def root():

    return {

        "status":"online",

        "server":"VoidVision",

        "version":"28"

    }



# ================= REGISTER =================


@app.post("/api/register")
async def register(data:dict):

    username = str(
        data.get("username","")
    ).strip()


    password = str(
        data.get("password","")
    ).strip()



    if len(username)<3:

        return {

            "success":False,

            "message":
            "username too short"

        }



    if len(password)<4:

        return {

            "success":False,

            "message":
            "password too short"

        }




    if username in users:

        return {

            "success":False,

            "message":
            "already exists"

        }



    hashed = bcrypt.hashpw(

        password.encode(),

        bcrypt.gensalt(10)

    )



    uid = str(uuid.uuid4())



    users[username]={

        "id":uid,

        "password":
        hashed.decode()

    }



    friends[username]=[]

    requests[username]=[]



    return {

        "success":True,

        "message":
        "registered"

    }





# ================= LOGIN =================


@app.post("/api/login")
async def login(data:dict):


    username=str(
        data.get("username","")
    ).strip()


    password=str(
        data.get("password","")
    ).strip()



    if username not in users:

        raise HTTPException(
            401,
            "wrong login"
        )



    user=users[username]



    ok=bcrypt.checkpw(

        password.encode(),

        user["password"].encode()

    )



    if not ok:

        raise HTTPException(
            401,
            "wrong login"
        )



    token=make_token(

        username,

        user["id"]

    )



    return {

        "access_token":token,

        "token_type":"bearer",

        "username":username,

        "user_id":
        user["id"]

    }




# ================= WEBSOCKET =================


class Manager:


    def __init__(self):

        self.connections=[]

        self.names={}




    async def add(
        self,
        ws,
        username
    ):

        self.connections.append(ws)

        self.names[ws]=username

        online_users[username]=ws




    def remove(self,ws):

        name=self.names.pop(
            ws,
            None
        )


        if name:

            online_users.pop(
                name,
                None
            )


        if ws in self.connections:

            self.connections.remove(ws)




    async def send(
        self,
        username,
        data
    ):

        ws=online_users.get(username)

        if ws:

            await ws.send_json(data)





    async def broadcast(
        self,
        data
    ):

        for ws in self.connections[:]:

            try:

                await ws.send_json(data)

            except:

                pass




manager=Manager()




@app.websocket("/ws")
async def websocket(ws:WebSocket):

    await ws.accept()


    current=None



    try:

        while True:


            raw=await ws.receive_text()


            msg=json.loads(raw)


            typ=msg.get("type")



            # ===== AUTH =====


            if typ=="auth":


                try:

                    payload=check_token(
                        msg.get("token")
                    )


                    username=payload["sub"]


                    if username not in users:

                        raise Exception()



                    await manager.add(
                        ws,
                        username
                    )


                    current=username



                    await ws.send_json({

                        "type":
                        "auth_response",

                        "success":
                        True,

                        "username":
                        username

                    })


                except:

                    await ws.send_json({

                        "type":
                        "auth_response",

                        "success":
                        False

                    })


                continue



            if not current:

                await ws.send_json({

                    "error":
                    "login first"

                })

                continue
            # ================= ROOM LIST =================

            if typ == "get_room_list":

                result = []

                for rid, room in rooms.items():

                    result.append({

                        "room_id": rid,

                        "room_name": room["name"],

                        "players":
                        len(room["players"]),

                        "max":
                        room["max"]

                    })


                await ws.send_json({

                    "type":
                    "room_list",

                    "rooms":
                    result

                })



            # ================= CREATE ROOM =================


            elif typ == "create_room":


                room_id = str(uuid.uuid4())[:6]


                rooms[room_id]={


                    "name":
                    msg.get(
                        "room_name",
                        "Room"
                    ),


                    "password":
                    msg.get(
                        "password",
                        ""
                    ),


                    "host":
                    current,


                    "players":[
                        current
                    ],


                    "max":
                    int(
                        msg.get(
                            "max_players",
                            8
                        )
                    ),


                    "key":
                    str(
                        uuid.uuid4()
                    )


                }



                await ws.send_json({

                    "type":
                    "room_created",


                    "room":{

                        "id":
                        room_id,


                        "key":
                        rooms[room_id]["key"]

                    }

                })



                await manager.broadcast({

                    "type":
                    "room_update"

                })





            # ================= JOIN ROOM =================


            elif typ=="join_room":


                rid=msg.get(
                    "room_id"
                )


                if rid not in rooms:


                    await ws.send_json({

                        "type":
                        "error",

                        "message":
                        "room not found"

                    })


                    continue




                room=rooms[rid]



                if room["password"]:


                    if room["password"] != msg.get("password",""):


                        await ws.send_json({

                            "type":
                            "error",

                            "message":
                            "wrong password"

                        })


                        continue

                if current not in room["players"]:


                    room["players"].append(
                        current
                    )



                await ws.send_json({

                    "type":
                    "room_joined",


                    "room":{

                        "id":
                        rid,


                        "players":
                        room["players"],


                        "host":
                        room["host"]

                    }

                })




                await manager.broadcast({

                    "type":
                    "room_players",


                    "players":
                    room["players"],


                    "host":
                    room["host"]

                })





            # ================= LEAVE =================


            elif typ=="leave_room":


                for rid,room in list(rooms.items()):


                    if current in room["players"]:


                        room["players"].remove(
                            current
                        )


                        if not room["players"]:


                            del rooms[rid]



                        break



                await ws.send_json({

                    "type":
                    "left_room"

                })





            # ================= WEBRTC OFFER =================


            elif typ=="offer":


                target=msg.get(
                    "target"
                )


                await manager.send(

                    target,

                    {

                    "type":
                    "offer_received",


                    "from":
                    current,


                    "sdp":
                    msg.get("sdp")

                    }

                )





            # ================= WEBRTC ANSWER =================


            elif typ=="answer":


                target=msg.get(
                    "target"
                )


                await manager.send(

                    target,

                    {

                    "type":
                    "answer_received",


                    "from":
                    current,


                    "sdp":
                    msg.get("sdp")

                    }

                )





            # ================= ICE =================


            elif typ=="ice_candidate":


                target=msg.get(
                    "target"
                )


                await manager.send(

                    target,

                    {

                    "type":
                    "ice_received",


                    "from":
                    current,


                    "candidate":
                    msg.get("candidate")

                    }

                )





            # ================= CHAT =================


            elif typ=="chat_message":


                await manager.broadcast({

                    "type":
                    "chat_message",


                    "sender":
                    current,


                    "message":
                    msg.get(
                        "message",
                        ""
                    )[:500]

                })





            # ================= FRIEND REQUEST =================


            elif typ=="friend_request":


                target=msg.get(
                    "target"
                )


                if target in users:


                    requests.setdefault(
                        target,
                        []
                    ).append(
                        current
                    )


                    await manager.send(

                        target,

                        {

                        "type":
                        "friend_request",


                        "from":
                        current

                        }

                    )





            elif typ=="friend_accept":


                target=msg.get(
                    "target"
                )


                friends.setdefault(
                    current,
                    []
                )


                friends.setdefault(
                    target,
                    []
                )



                if target not in friends[current]:

                    friends[current].append(
                        target
                    )


                if current not in friends[target]:

                    friends[target].append(
                        current
                    )


                await manager.send(

                    target,

                    {

                    "type":
                    "friend_added",


                    "user":
                    current

                    }

                )





            # ================= READY =================


            elif typ=="ready_to_launch":


                await manager.broadcast({

                    "type":
                    "player_ready",


                    "player":
                    current

                })





            # ================= LAUNCH =================


            elif typ=="launch_game":


                rid=msg.get(
                    "room_id"
                )


                room=rooms.get(
                    rid
                )


                if room and room["host"]==current:


                    await manager.broadcast({

                        "type":
                        "launch_game_command",


                        "game":
                        msg.get(
                            "game_name"
                        )

                    })



            else:


                await ws.send_json({

                    "type":
                    "unknown",

                    "value":
                    typ

                })




    except WebSocketDisconnect:


        manager.remove(ws)


        if current:

            await manager.broadcast({

                "type":
                "user_list",

                "users":
                list(
                    online_users.keys()
                )

            })





# ================= RUN =================


if __name__=="__main__":


    import uvicorn


    port=int(
        os.getenv(
            "PORT",
            8000
        )
    )


    uvicorn.run(

        app,

        host="0.0.0.0",

        port=port

    )
