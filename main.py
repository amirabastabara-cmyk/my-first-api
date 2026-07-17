# main.py
# VoidVision Server v30
# FastAPI + SQLite + WebSocket

import os
import json
import uuid
import sqlite3
import bcrypt
import jwt

from datetime import datetime, timedelta

from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException
)

from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================
# APP
# =========================

app = FastAPI(
    title="VoidVision Server",
    version="30.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# CONFIG
# =========================

JWT_SECRET = os.getenv(
    "JWT_SECRET",
    "voidvision-secret-change"
)

JWT_ALGORITHM = "HS256"

TOKEN_TIME = 60 * 24 * 7


DATABASE = "voidvision.db"



# =========================
# DATABASE
# =========================


def db():

    conn = sqlite3.connect(
        DATABASE,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn



def init_db():

    con = db()

    cur = con.cursor()


    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(

        id TEXT PRIMARY KEY,
        username TEXT UNIQUE,
        password TEXT

    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS friends(

        user TEXT,
        friend TEXT

    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests(

        sender TEXT,
        receiver TEXT

    )
    """)


    con.commit()

    con.close()



init_db()



# =========================
# MODELS
# =========================


class RegisterModel(BaseModel):

    username:str
    password:str



class LoginModel(BaseModel):

    username:str
    password:str




# =========================
# JWT
# =========================


def create_token(username, uid):

    payload = {

        "sub": username,

        "uid": uid,

        "exp":
        datetime.utcnow()
        +
        timedelta(
            minutes=TOKEN_TIME
        )

    }


    return jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )



def decode_token(token):

    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=[
            JWT_ALGORITHM
        ]
    )



# =========================
# REGISTER
# =========================


@app.post("/api/register")
async def register(data:RegisterModel):


    username = data.username.strip()

    password = data.password.strip()



    if len(username)<3:

        return {
            "success":False,
            "message":"Username too short"
        }



    if len(password)<4:

        return {
            "success":False,
            "message":"Password too short"
        }



    con=db()

    cur=con.cursor()



    cur.execute(
        "SELECT username FROM users WHERE username=?",
        (username,)
    )


    if cur.fetchone():

        con.close()

        return {

            "success":False,

            "message":"Username exists"

        }




    uid=str(uuid.uuid4())



    hashed=bcrypt.hashpw(
        password.encode(),
        bcrypt.gensalt()
    )



    cur.execute(
        """
        INSERT INTO users
        VALUES(?,?,?)
        """,
        (
            uid,
            username,
            hashed.decode()
        )
    )


    con.commit()

    con.close()



    return {

        "success":True,

        "message":"Account created"

    }





# =========================
# LOGIN
# =========================


@app.post("/api/login")
async def login(data:LoginModel):


    username=data.username.strip()

    password=data.password.strip()



    con=db()

    cur=con.cursor()



    cur.execute(
        """
        SELECT *
        FROM users
        WHERE username=?
        """,
        (username,)
    )


    user=cur.fetchone()


    con.close()



    if not user:

        raise HTTPException(
            401,
            "Wrong username or password"
        )



    stored=user["password"].encode()



    if not bcrypt.checkpw(
        password.encode(),
        stored
    ):


        raise HTTPException(
            401,
            "Wrong username or password"
        )




    token=create_token(
        username,
        user["id"]
    )



    return {


        "access_token":token,

        "token_type":"bearer",

        "username":username,

        "user_id":user["id"]

    }





# =========================
# ONLINE SYSTEM
# =========================


online_users={}



class ConnectionManager:


    def __init__(self):

        self.connections=[]



    async def connect(
        self,
        ws,
        username
    ):

        await ws.accept()

        self.connections.append(ws)

        online_users[username]=ws




    def disconnect(
        self,
        ws
    ):


        for name,sock in list(
            online_users.items()
        ):

            if sock==ws:

                del online_users[name]



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


        for ws in self.connections:

            try:

                await ws.send_json(data)

            except:

                pass



manager=ConnectionManager()



# =========================
# WEBSOCKET
# =========================


@app.websocket("/ws")
async def websocket(ws:WebSocket):


    await ws.accept()


    current=None



    try:


        while True:


            raw=await ws.receive_text()


            msg=json.loads(raw)


            t=msg.get("type")



            # AUTH


            if t=="auth":


                token=msg.get("token")


                try:


                    data=decode_token(token)


                    current=data["sub"]



                    await manager.connect(
                        ws,
                        current
                    )



                    await ws.send_json({

                        "type":"auth_response",

                        "success":True,

                        "username":current

                    })



                    await manager.broadcast({

                        "type":"user_list",

                        "users":
                        list(
                            online_users.keys()
                        )

                    })



                except Exception as e:


                    await ws.send_json({

                        "type":"auth_response",

                        "success":False,

                        "message":str(e)

                    })



                continue



            if not current:


                await ws.send_json({

                    "type":"error",

                    "message":"Login first"

                })


                continue
            # جلوگیری از استفاده بدون لاگین

            if not current:
                await ws.send_json({
                    "type":"error",
                    "message":"not authenticated"
                })
                continue



            # ================= ROOMS =================


            if typ=="get_room_list":

                data=[]

                for rid,room in rooms.items():

                    data.append({

                        "room_id":rid,
                        "room_name":room["name"],
                        "players":len(room["players"]),
                        "max":room["max"]

                    })


                await ws.send_json({

                    "type":"room_list",
                    "rooms":data

                })



            elif typ=="create_room":


                room_id=str(uuid.uuid4())[:8]


                rooms[room_id]={

                    "name":msg.get(
                        "room_name",
                        "Void Room"
                    ),

                    "password":msg.get(
                        "password",
                        ""
                    ),

                    "max":msg.get(
                        "max_players",
                        8
                    ),

                    "host":current,

                    "players":[current],

                    "key":str(uuid.uuid4())

                }


                await ws.send_json({

                    "type":"room_created",

                    "room":{

                        "room_id":room_id,

                        "room_key":
                        rooms[room_id]["key"],

                        "subnet":
                        "10.77.0."

                    }

                })



                await manager.broadcast({

                    "type":"room_update"

                })





            elif typ=="join_room":


                rid=msg.get("room_id")


                if rid not in rooms:

                    await ws.send_json({

                        "type":"room_joined",

                        "success":False,

                        "message":"room not found"

                    })

                    continue



                room=rooms[rid]


                if room["password"]:

                    if room["password"] != msg.get("password",""):

                        await ws.send_json({

                            "type":"room_joined",

                            "success":False,

                            "message":"wrong password"

                        })

                        continue



                if current not in room["players"]:

                    if len(room["players"]) >= room["max"]:

                        await ws.send_json({

                            "type":"room_joined",

                            "success":False,

                            "message":"room full"

                        })

                        continue


                    room["players"].append(current)




                ips={}


                for i,p in enumerate(room["players"]):

                    ips[p]=f"10.77.0.{i+1}"




                await ws.send_json({

                    "type":"room_joined",

                    "success":True,

                    "room":{

                        "room_id":rid,

                        "key":room["key"],

                        "players":room["players"],

                        "ips":ips,

                        "host":room["host"]

                    }

                })



                await manager.broadcast({

                    "type":"room_players",

                    "room":rid,

                    "players":room["players"],

                    "ips":ips,

                    "host":room["host"]

                })







            elif typ=="leave_room":


                for rid,room in list(rooms.items()):


                    if current in room["players"]:


                        room["players"].remove(current)


                        if len(room["players"])==0 or room["host"]==current:

                            del rooms[rid]


                            await manager.broadcast({

                                "type":"room_closed",

                                "room_id":rid

                            })



                        break



                await ws.send_json({

                    "type":"left_room"

                })






            # ================= WEBRTC SIGNAL =================



            elif typ=="offer":


                target=msg.get("target")


                await manager.send(

                    target,

                    {

                    "type":"offer_received",

                    "from":current,

                    "sdp":msg.get("sdp"),

                    "game":msg.get("game_name","")

                    }

                )




            elif typ=="answer":


                await manager.send(

                    msg.get("target"),

                    {

                    "type":"answer_received",

                    "from":current,

                    "sdp":msg.get("sdp")

                    }

                )





            elif typ=="ice_candidate":


                await manager.send(

                    msg.get("target"),

                    {

                    "type":"ice_received",

                    "from":current,

                    "candidate":msg.get("candidate")

                    }

                )





            # ================= CHAT =================



            elif typ=="chat_message":


                text=msg.get(
                    "message",
                    ""
                )[:500]


                await manager.broadcast({

                    "type":"chat_message",

                    "sender":current,

                    "message":text

                })






            # ================= FRIEND SYSTEM =================



            elif typ=="friend_request":


                target=msg.get("target")


                if target in users:


                    friend_requests.setdefault(
                        target,
                        []
                    ).append(current)


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


                target=msg.get("target")


                if target in friend_requests.get(
                    current,
                    []
                ):


                    friend_requests[current].remove(
                        target
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

                    friends[current].append(target)



                if current not in friends[target]:

                    friends[target].append(current)



                await ws.send_json({

                    "type":"friends",

                    "list":friends[current]

                })






            elif typ=="friend_reject":


                target=msg.get("target")


                if target in friend_requests.get(current,[]):

                    friend_requests[current].remove(target)







            # ================= GAME LAUNCH =================



            elif typ=="ready_to_launch":


                await manager.broadcast({

                    "type":"player_ready",

                    "player":current,

                    "room":msg.get("room_id")

                })






            elif typ=="launch_game":


                rid=msg.get("room_id")


                room=rooms.get(rid)



                if room and room["host"]==current:


                    await manager.broadcast({

                        "type":"launch_game",

                        "game":
                        msg.get("game_name"),

                        "room":
                        rid

                    })







            else:


                await ws.send_json({

                    "type":"error",

                    "message":
                    "unknown command"

                })




    except WebSocketDisconnect:

        manager.remove(ws)


        if current:

            await manager.broadcast({

                "type":"users",

                "list":
                list(online_users.keys())

            })





# ================= START =================


if __name__=="__main__":

    import uvicorn


    uvicorn.run(

        app,

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                8000
            )
        )

    )
