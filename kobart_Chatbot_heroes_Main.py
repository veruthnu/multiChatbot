import argparse
import logging
import os
import yaml
import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from random import *
import time
import tkinter
from tkinter import *
import os
from pytorch_lightning import loggers as pl_loggers
from torch.utils.data import DataLoader, Dataset
from transformers import (BartForConditionalGeneration,
                          PreTrainedTokenizerFast)
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup
#import teacher_v1 as ET

parser = argparse.ArgumentParser(description='KoBART Chit-Chat')

parser.add_argument('--subtask',
                    type=str,
                    default='NSMC',
                    help='NSMC, CoLA, QPair')

parser.add_argument('--checkpoint_path',
                    type=str,
                    help='checkpoint path')

parser.add_argument('--chat',
                    action='store_true',
                    default=False,
                    help='response generation on given user input')

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ArgsBase():
    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)
        parser.add_argument('--train_file',
                            type=str,
                            default='Chatbot_data/train.csv',
                            help='train file')

        parser.add_argument('--test_file',
                            type=str,
                            default='Chatbot_data/test.csv',
                            help='test file')

        parser.add_argument('--tokenizer_path',
                            type=str,
                            default='tokenizer',
                            help='tokenizer')
        parser.add_argument('--batch_size',
                            type=int,
                            default=14,
                            help='')
        parser.add_argument('--max_seq_len',
                            type=int,
                            default=36,
                            help='max seq len')

        parser.add_argument('--hparams',
                            default=None,
                            type=str,
                            help='hparams')

        return parser


class ChatDataset(Dataset):
    def __init__(self, filepath, tok_vocab, max_seq_len=128) -> None:
        self.filepath = filepath
        self.data = pd.read_csv(self.filepath, encoding='cp949')
        self.bos_token = '<s>'
        self.eos_token = '</s>'
        self.max_seq_len = max_seq_len
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_file=tok_vocab,
            bos_token=self.bos_token, eos_token=self.eos_token, unk_token='<unk>', pad_token='<pad>', mask_token='<mask>')

    def __len__(self):
        return len(self.data)

    def make_input_id_mask(self, tokens, index):
        input_id = self.tokenizer.convert_tokens_to_ids(tokens)
        attention_mask = [1] * len(input_id)
        if len(input_id) < self.max_seq_len:
            while len(input_id) < self.max_seq_len:
                input_id += [self.tokenizer.pad_token_id]
                attention_mask += [0]
        else:
            # logging.warning(f'exceed max_seq_len for given article : {index}')
            input_id = input_id[:self.max_seq_len - 1] + [
                self.tokenizer.eos_token_id]
            attention_mask = attention_mask[:self.max_seq_len]
        return input_id, attention_mask

    def __getitem__(self, index):
        record = self.data.iloc[index]
        q, a = record['Q'], record['A']
        q_tokens = [self.bos_token] + \
            self.tokenizer.tokenize(q) + [self.eos_token]
        a_tokens = [self.bos_token] + \
            self.tokenizer.tokenize(a) + [self.eos_token]
        encoder_input_id, encoder_attention_mask = self.make_input_id_mask(
            q_tokens, index)
        decoder_input_id, decoder_attention_mask = self.make_input_id_mask(
            a_tokens, index)
        labels = self.tokenizer.convert_tokens_to_ids(
            a_tokens[1:(self.max_seq_len + 1)])
        if len(labels) < self.max_seq_len:
            while len(labels) < self.max_seq_len:
                # for cross entropy loss masking
                labels += [-100]
        return {'input_ids': np.array(encoder_input_id, dtype=np.int_),
                'attention_mask': np.array(encoder_attention_mask, dtype=np.float),
                'decoder_input_ids': np.array(decoder_input_id, dtype=np.int_),
                'decoder_attention_mask': np.array(decoder_attention_mask, dtype=np.float),
                'labels': np.array(labels, dtype=np.int_)}


class ChatDataModule(pl.LightningDataModule):
    def __init__(self, train_file,
                 test_file, tok_vocab,
                 max_seq_len=128,
                 batch_size=32,
                 num_workers=5):

        super().__init__()
        self.batch_size = batch_size
        self.max_seq_len = max_seq_len
        self.train_file_path = train_file
        self.test_file_path = test_file
        self.tok_vocab = tok_vocab
        self.num_workers = num_workers

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)
        parser.add_argument('--num_workers',
                            type=int,
                            default=5,
                            help='num of worker for dataloader')
        return parser

    # OPTIONAL, called for every GPU/machine (assigning state is OK)
    def setup(self, stage):
        # split dataset
        self.train = ChatDataset(self.train_file_path,
                                 self.tok_vocab,
                                 self.max_seq_len)
        self.test = ChatDataset(self.test_file_path,
                                self.tok_vocab,
                                self.max_seq_len)

    def train_dataloader(self):
        train = DataLoader(self.train,
                           batch_size=self.batch_size,
                           num_workers=self.num_workers, shuffle=True)
        return train

    def val_dataloader(self):
        val = DataLoader(self.test,
                         batch_size=self.batch_size,
                         num_workers=self.num_workers, shuffle=False)
        return val

    def test_dataloader(self):
        test = DataLoader(self.test,
                          batch_size=self.batch_size,
                          num_workers=self.num_workers, shuffle=False)
        return test


class Base(pl.LightningModule):
    def __init__(self, hparams, **kwargs) -> None:
        super(Base, self).__init__()
        self.hparams = hparams

    @staticmethod
    def add_model_specific_args(parent_parser):
        # add model specific args
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)

        parser.add_argument('--batch-size',
                            type=int,
                            default=14,
                            help='batch size for training (default: 96)')

        parser.add_argument('--lr',
                            type=float,
                            default=5e-5,
                            help='The initial learning rate')

        parser.add_argument('--warmup_ratio',
                            type=float,
                            default=0.1,
                            help='warmup ratio')

        parser.add_argument('--model_path',
                            type=str,
                            default=None,
                            help='kobart model path')
        return parser

    def configure_optimizers(self):
        # Prepare optimizer
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(
                nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(
                nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=self.hparams.lr, correct_bias=False)
        # warm up lr
        num_workers = (self.hparams.gpus if self.hparams.gpus is not None else 1) * (self.hparams.num_nodes if self.hparams.num_nodes is not None else 1)
        data_len = len(self.train_dataloader().dataset)
        logging.info(f'number of workers {num_workers}, data length {data_len}')
        num_train_steps = int(data_len / (self.hparams.batch_size * num_workers) * self.hparams.max_epochs)
        logging.info(f'num_train_steps : {num_train_steps}')
        num_warmup_steps = int(num_train_steps * self.hparams.warmup_ratio)
        logging.info(f'num_warmup_steps : {num_warmup_steps}')
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=num_warmup_steps, num_training_steps=num_train_steps)
        lr_scheduler = {'scheduler': scheduler, 
                        'monitor': 'loss', 'interval': 'step',
                        'frequency': 1}
        return [optimizer], [lr_scheduler]


class KoBARTConditionalGeneration(Base):
    def __init__(self, hparams, **kwargs):
        super(KoBARTConditionalGeneration, self).__init__(hparams, **kwargs)
        self.model = BartForConditionalGeneration.from_pretrained(self.hparams.model_path)
        self.model.train()
        self.bos_token = '<s>'
        self.eos_token = '</s>'
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_file=os.path.join(self.hparams.tokenizer_path, 'model.json'),
            bos_token=self.bos_token, eos_token=self.eos_token, unk_token='<unk>', pad_token='<pad>', mask_token='<mask>')

    def forward(self, inputs):
        return self.model(input_ids=inputs['input_ids'],
                          attention_mask=inputs['attention_mask'],
                          decoder_input_ids=inputs['decoder_input_ids'],
                          decoder_attention_mask=inputs['decoder_attention_mask'],
                          labels=inputs['labels'], return_dict=True)

    def training_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs.loss
        self.log('train_loss', loss, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs['loss']
        return (loss)

    def validation_epoch_end(self, outputs):
        losses = []
        for loss in outputs:
            losses.append(loss)
        self.log('val_loss', torch.stack(losses).mean(), prog_bar=True)

    def chat(self, text):
        input_ids =  [self.tokenizer.bos_token_id] + self.tokenizer.encode(text) + [self.tokenizer.eos_token_id]

        res_ids = self.model.generate(torch.tensor([input_ids]),
                                            max_length=self.hparams.max_seq_len,
                                            num_beams=5,
                                            eos_token_id=self.tokenizer.eos_token_id,
                                            bad_words_ids=[[self.tokenizer.unk_token_id]])        
        a = self.tokenizer.batch_decode(res_ids.tolist())[0]
        return a.replace('<s>', '').replace('</s>', '').replace('<usr>', '')

#??????
Qfilepath = 'Chatbot_data/quizfinal.csv'

class Englishteacher:

    global Qlen

    def __init__(self,filepath):
        self.filepath = Qfilepath
        self.Question = pd.read_csv(self.filepath)
        self.Answer = pd.read_csv(self.filepath)
        Kkangtong("?????????????????? ???????????????")
        

    def EnglishtQuestion(self):

        global Qlen
        Question_E = self.Question['Q']
        Qlen = randint(0,len(Question_E))

        return Question_E[Qlen]
    
    def EnglishAnswer(self):

        global Qlen
        Question_A = self.Question['A']

        return Question_A[Qlen]


#????????? ?????? ????????? ????????????????????? 
#???????????? ??????................................................................................
def ENGLISH_TEACHER():
    num = ['?????????', '?????????', '?????????', '?????????', '????????????','????????????',
    '????????????', '????????????', '????????????', '?????????' ]

    #while 1:
    falsecount = 0
    score = 0
    now = time.localtime()
    global Et
    Et = Englishteacher(Qfilepath)

    for i in range(1):

        Kkangtong(num[i]+' ?????? ??????')
        Kkangtong(Et.EnglishtQuestion())
        for falsecount in range(2):
            base.after(1000)
            Kkangtong("????????? ????????? ?????? ?????????\n\#(???????????? ????????? '??????' ????????? ???????????????): ")
            
           
    
            base.after(2000)
            base.bind_all('<KeyPress-Return>', message_et)
            base.after(2000)
            print('bind ??????')
            #base.mainloop()
            print('mainloop ??????')
            #if Answer != '':
            if msg == Et.EnglishAnswer():
                Kkangtong("?????? ???????????? ????????????")
                score +=1
                break
            elif falsecount < 1:
                Kkangtong("???????????? ????????? ???????????? ?????? ???????????????")
                falsecount += 1
                continue
            else:
                Kkangtong(" ??????????????? ?????? {} ?????????".format(Et.EnglishAnswer()))
                break

            if msg == '??????':
                Kkangtong('????????? ???????????? ??????, ????????????')
                break
            base.mainloop()
    Kkangtong("???????????? ?????????")
    Kkangtong("%2d???%2d??? %02d???%02d??? ????????? ??????????????? %d ?????????"%(now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min,score*10))


f = lambda x, *args : x * args
a = f(20, 50)


#def message_insert():
#        msg = EntryBox.get("1.0",'end-1c').strip()
#        EntryBox.delete("0.0",END)

#        return msg
def message_et(event):
    global msg
    msg = EntryBox.get("1.0",'end-1c').strip()
    EntryBox.delete("0.0",END)

    ChatLog.config(state=NORMAL)

    ChatLog.tag_configure('tag-right', justify='right') 
    ChatLog.tag_configure('tag-left', justify='left') 
                
    ChatLog.insert(END,'\n ','tag-right')
                
    ChatLog.window_create(END, window=Label(ChatLog, fg="#000000", text=msg, 
    wraplength=200, font=("Arial", 13), bg="lightblue", bd=4, justify="left"))
                
    ChatLog.config(foreground="#442265", font=("Verdana", 12 ))
                
    ChatLog.yview(END)

    ChatLog.insert(END, '\n ', 'tag-left')
    print('???????????????')

    #return msg
    #base.destroy()

#????????? ????????? ????????? 
def message():
    global msg
    msg = EntryBox.get("1.0",'end-1c').strip()
    EntryBox.delete("0.0",END)

    ChatLog.config(state=NORMAL)

    ChatLog.tag_configure('tag-right', justify='right') 
    ChatLog.tag_configure('tag-left', justify='left') 
                
    ChatLog.insert(END,'\n ','tag-right')
                
    ChatLog.window_create(END, window=Label(ChatLog, fg="#000000", text=msg, 
    wraplength=200, font=("Arial", 13), bg="lightblue", bd=4, justify="left"))
                
    ChatLog.config(foreground="#442265", font=("Verdana", 12 ))
                
    ChatLog.yview(END)

    ChatLog.insert(END, '\n ', 'tag-left')
    print('???????????????')

    return msg

## ??????1
def Kkangtong(msg):

    image = tkinter.PhotoImage(file="robot.png").subsample(7,7)

    label = tkinter.Label(ChatLog, text='??????',image=image) 

    label.image = image

    ChatLog.window_create(END, window=label)

    ChatLog.insert(END, ' ')
                        
    ChatLog.window_create(END, window=Label(ChatLog, fg="#000000", text=msg, 
    wraplength=200, font=("Arial", 13), bg="#DDDDDD", bd=4, justify="left"))

    ChatLog.insert(END, '\n\n ')

#??????
def Baqui(msg):
    #res1 = model.chat(msg)

    image1 = tkinter.PhotoImage(file="robot2.png").subsample(7,7)
    label1 = tkinter.Label(ChatLog, text='??????', image=image1)
    label1.image = image1

    ChatLog.window_create(END, window = label1)
    ChatLog.insert(END, ' ')


    ChatLog.window_create(END, window=Label(ChatLog, fg="#000000", text=msg, 
    wraplength=200, font=("Arial", 13), bg="pink", bd=4, justify="left"))

    ChatLog.insert(END, '\n ', "tag-right")

    ChatLog.config(state=DISABLED)
                   
##????????? 
def send(event):
    print('send ??????')
    msg = message()
    if  msg =='????????????':
        ENGLISH_TEACHER()
        print('send ??????')

    elif msg != '':
        res = model.chat(msg)
        Kkangtong(res)
        res1 = model.chat(msg)
        Baqui(res1)
        print('send ??????')

if __name__ == '__main__':
    parser = Base.add_model_specific_args(parser)
    parser = ArgsBase.add_model_specific_args(parser)
    parser = ChatDataModule.add_model_specific_args(parser)
    parser = pl.Trainer.add_argparse_args(parser)
    args = parser.parse_args()
    logging.info(args)

    with open('logs/tb_logs/default/version_0/hparams.yaml') as f:
        hparams = yaml.load(f)

        #model = KoBARTConditionalGeneration(args)
    model = KoBARTConditionalGeneration.load_from_checkpoint('logs/kobart_chitchat-model_chp/Kkangtong.ckpt', hparams=hparams)
    dm = ChatDataModule(args.train_file,
                        args.test_file,
                        os.path.join(args.tokenizer_path, 'model.json'),
                        max_seq_len=args.max_seq_len,
                        num_workers=args.num_workers)
    checkpoint_callback = pl.callbacks.ModelCheckpoint(monitor='val_loss',
                                                        dirpath=args.default_root_dir,
                                                        filename='model_chp/{epoch:02d}-{val_loss:.3f}',
                                                        verbose=True,
                                                        save_last=True,
                                                        mode='min',
                                                        save_top_k=-1, #save_top_k=-1??? ?????? ??????
                                                        prefix='kobart_chitchat')
    tb_logger = pl_loggers.TensorBoardLogger(os.path.join(args.default_root_dir, 'tb_logs'))
    lr_logger = pl.callbacks.LearningRateMonitor()
    trainer = pl.Trainer.from_argparse_args(args, logger=tb_logger,
                                            callbacks=[checkpoint_callback, lr_logger])

            #trainer.fit(model, dm)

    #model.model.eval()

            #creating GUI with tkinter

    base = Tk()
    base.title('KKANGTONG & BAQUI')
    base.geometry('800x1000')
    base.resizable(False, False)

    ChatLog = Text(base, bd=0, bg="white", height="8", width="50", font="Arial",)

    ChatLog.config(state=DISABLED)

        #Bind scrollbar to Chat window
    scrollbar = Scrollbar(base, command=ChatLog.yview, cursor="heart")
    ChatLog['yscrollcommand'] = scrollbar.set

        #Create Button to send message
    SendButton = Button(base, font=("Verdana",12,'bold'), text="Send", width="12", height=5,
                            bd=0, bg="skyblue", activebackground="#3c9d9b",fg='#ffffff', command= send )

    #Create the box to enter message
    ##32de97
    EntryBox = Text(base, bd=0, bg="white",width="29", height="5", font="Arial")

    #??????
    #Place all components on the screen
    scrollbar.place(x=780, y=10, height=772) 
    ChatLog.place(x=10,y=10, height=810, width=760) 
    EntryBox.place(x=6, y=830, height=160, width=530) 
    SendButton.place(x=540, y=830 , height=160, width=250)
    
    base.bind_all('<KeyPress-Return>', send)
    base.mainloop()

    #Create Chat window
'''
def GUI():
    ChatLog = Text(base, bd=0, bg="white", height="8", width="50", font="Arial",)

    ChatLog.config(state=DISABLED)

        #Bind scrollbar to Chat window
    scrollbar = Scrollbar(base, command=ChatLog.yview, cursor="heart")
    ChatLog['yscrollcommand'] = scrollbar.set

        #Create Button to send message
    SendButton = Button(base, font=("Verdana",12,'bold'), text="Send", width="12", height=5,
                            bd=0, bg="skyblue", activebackground="#3c9d9b",fg='#ffffff', command= send )

                #Create the box to enter message
                ##32de97
    EntryBox = Text(base, bd=0, bg="white",width="29", height="5", font="Arial")
            #EntryBox.bind("<Return>", send)

                    #??????
                    #Place all components on the screen
    scrollbar.place(x=780, y=10, height=772) 
    ChatLog.place(x=10,y=10, height=810, width=760) 
    EntryBox.place(x=6, y=830, height=160, width=530) 
    SendButton.place(x=540, y=830 , height=160, width=250)



                        #Place all components on the screen
    
                        scrollbar.place(x=376,y=6, height=386) 
                        ChatLog.place(x=6,y=110, height=286, width=365) 
                        EntryBox.place(x=6, y=401, height=90, width=265)
                        SendButton.place(x=282, y=401, height=90, width = 95)

                        image = tkinter.PhotoImage(file="C:/Users/lunal/Desktop/kkang.gif")
                        label = tkinter.Label(base, image=image)
                        label.pack()
'''


                        #if args.chat:
                        #    model.model.eval()
                #    while 1:
                #        q = input('user > ').strip()
            #        if q == 'quit':
            #            break
            #        print("Kkangtong > {}".format(model.chat(q)))