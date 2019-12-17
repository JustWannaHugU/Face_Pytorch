import os

def generate(dir,label,save_list= None):
    files = os.listdir(dir)
    print ('start dealing dir:',dir)
    listText = open('train.list','a')
    for file in files:
        fileType = os.path.split(file)
        print('fileType = ',fileType)
        if fileType[1] == '.list':
            continue
        name = dir + '\\' + file + ' '  + str(int(label)).zfill(6) +'\n'
        print('file = ',file)
        print('name = ',name)
        listText.write(name)
    listText.close()
    print ('dealing over!')
    print()
 
 
outer_path = r'E:\Insightface-PYTORCH\MS-celeb-1M\faces_webface\faces_webface_112x112\faces_webface_my'
 
 
if __name__ == '__main__':
    i = 0
    folderlist = os.listdir(outer_path)          
    for folder in folderlist:            
        generate(os.path.join(outer_path, folder),i)
        i += 1
